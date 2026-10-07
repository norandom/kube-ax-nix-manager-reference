"""Cluster verification (Requirements 8.4, 11.3-11.5, 13.16).

    just verify

Per node (over SSH): CPU/KVM, swap mode, key sysctls, k3s unit protection.
Once, against the cluster (kubectl with kubeconfig/<cluster>.yaml): nodes Ready, system pods
healthy, pod spread across all nodes, ingress via the server IP to a pod on an agent,
LoadBalancer ports closed on agents. Test objects live in a temporary namespace.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from pyinfra import host, inventory, logger
from pyinfra.facts.server import Command
from pyinfra.operations import python

from lib.cluster import nodes
from lib.facts import CpuCapabilities, TuningState

ROOT = Path(__file__).resolve().parent.parent
NS = "kube-ax-verify"
EXPECTED_SYSCTLS = {
    "vm.swappiness": "100",
    "vm.page-cluster": "0",
    "vm.overcommit_memory": "1",
    "kernel.panic": "10",
    "net.core.default_qdisc": "fq",
    "net.ipv4.tcp_congestion_control": "bbr",
    "net.ipv4.ip_forward": "1",
    "net.ipv4.conf.all.proxy_arp": "1",
}


class VerifyError(AssertionError):
    pass


def check(ok: bool, what: str) -> None:
    if not ok:
        raise VerifyError(what)
    logger.info("  ok  %s", what)


# --- per node -------------------------------------------------------------------------------


def node_checks(
    node: str, cpu: dict, tuning: dict, swap_mode: str, loaded: str, mem_kib: int, mem_gib: int
) -> None:
    name = node
    # Booted size minus kernel reservations is ~96% of the VM size; less means a ballooned guest.
    check(
        mem_kib >= mem_gib * 1024 * 1024 * 0.93,
        f"{name}: MemTotal {mem_kib // 1024} MiB >= ~{mem_gib} GiB (balloon min = max in Proxmox)",
    )
    check(cpu["avx2"] and cpu["fma"], f"{name}: AVX2 + FMA exposed")
    check(cpu["dev_kvm"], f"{name}: /dev/kvm present (micro-VM sandboxes)")
    swaps = {s["type"] for s in tuning["swap"]}
    if swap_mode == "zram":
        check(any(s["name"].startswith("/dev/zram") for s in tuning["swap"]), f"{name}: zram swap")
    elif swap_mode == "none":
        check(not swaps, f"{name}: no swap")
    for key, want in EXPECTED_SYSCTLS.items():
        have = tuning["sysctl"].get(key)
        check(have == want, f"{name}: {key} = {want} (is {have})")
    check(loaded == "loaded", f"{name}: k3s unit exists")
    check(tuning["k3s_oom_score_adjust"] == "-999", f"{name}: k3s OOMScoreAdjust -999")


def disk_checks(node: str, disk: str, ate_on_containerd: bool) -> None:
    """Free space on / and the containerd volume, Substrate image cache size (2026-10-05 outage)."""
    rows = {}
    for line in disk.splitlines():
        parts = line.split()
        if len(parts) == 4:
            rows[parts[0]] = (int(parts[1]), int(parts[2]), parts[3])
    for target in ("/", "/var/lib/rancher/k3s/agent/containerd"):
        size, avail, _ = rows.get(target, (1, 0, ""))
        check(
            avail * 100 // size >= 25,
            f"{node}: {target} {avail * 100 // size}% free (>= 25%; kubelet evicts below ~10%)",
        )
    cache = int(rows.get("cache", (0, 0, "0"))[2])
    check(cache <= 11 * 1024**3, f"{node}: actor image cache {cache >> 20} MiB (<= 11 GiB, cap 10)")
    if ate_on_containerd:
        src = rows.get("ate", (0, 0, ""))[2]
        check("containerd" in src, f"{node}: /var/lib/ate on the containerd volume ({src})")


# --- cluster --------------------------------------------------------------------------------


def kubectl(*args: str, stdin: str | None = None, check_rc: bool = True) -> str:
    cluster = host.data.get("cluster_name")
    out = subprocess.run(
        ["kubectl", "--kubeconfig", str(ROOT / "kubeconfig" / f"{cluster}.yaml"), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )
    if check_rc and out.returncode != 0:
        raise VerifyError(f"kubectl {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout


def http_status(url: str, host_header: str, timeout: float = 3) -> int | None:
    req = urllib.request.Request(url, headers={"Host": host_header})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return None


def manifests(node_names: list[str], agent: str | None, server: str) -> str:
    labels = {"app": "spread"}
    spread = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "spread", "namespace": NS},
        "spec": {
            "replicas": len(node_names),
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "topologySpreadConstraints": [
                        {
                            "maxSkew": 1,
                            "topologyKey": "kubernetes.io/hostname",
                            "whenUnsatisfiable": "DoNotSchedule",
                            "labelSelector": {"matchLabels": labels},
                        }
                    ],
                    "containers": [
                        {
                            "name": "pause",
                            "image": "registry.k8s.io/pause:3.10",
                            "resources": {"requests": {"cpu": "10m", "memory": "8Mi"}},
                        }
                    ],
                },
            },
        },
    }
    web_labels = {"app": "web"}
    web = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "web", "namespace": NS},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": web_labels},
            "template": {
                "metadata": {"labels": web_labels},
                "spec": {
                    "nodeSelector": {"kubernetes.io/hostname": agent or server},
                    "containers": [
                        {
                            "name": "whoami",
                            "image": "traefik/whoami:v1.11",
                            "ports": [{"containerPort": 80}],
                            "readinessProbe": {"httpGet": {"path": "/", "port": 80}},
                        }
                    ],
                },
            },
        },
    }
    svc = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "web", "namespace": NS},
        "spec": {"selector": web_labels, "ports": [{"port": 80, "targetPort": 80}]},
    }
    ing = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {"name": "web", "namespace": NS},
        "spec": {
            "rules": [
                {
                    "host": "verify.kube-ax.local",
                    "http": {
                        "paths": [
                            {
                                "path": "/",
                                "pathType": "Prefix",
                                "backend": {"service": {"name": "web", "port": {"number": 80}}},
                            }
                        ]
                    },
                }
            ]
        },
    }
    items = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NS}},
        spread,
        web,
        svc,
        ing,
    ]
    return json.dumps({"apiVersion": "v1", "kind": "List", "items": items})


def cluster_checks(
    server_addr: str, inventory_nodes: list[tuple[str, dict]], node_mem_kib: dict[str, int]
) -> None:
    names = [d["node_name"] for _, d in inventory_nodes]
    agents = [(a, d["node_name"]) for a, d in inventory_nodes if d["role"] == "agent"]
    server = next(d["node_name"] for _, d in inventory_nodes if d["role"] == "server")

    ready = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    status = {
        n["metadata"]["name"]: next(
            c["status"] for c in n["status"]["conditions"] if c["type"] == "Ready"
        )
        for n in ready
    }
    for name in names:
        check(status.get(name) == "True", f"node {name} Ready")
    for n in ready:
        name = n["metadata"]["name"]
        cap_kib = int(n["status"]["capacity"]["memory"].removesuffix("Ki"))
        check(not n["spec"].get("unschedulable"), f"node {name} schedulable (not cordoned)")
        check(
            cap_kib >= node_mem_kib.get(name, 0) * 0.98,
            f"node {name}: kubelet capacity {cap_kib // 1024} MiB matches RAM (else restart k3s)",
        )

    pods = json.loads(kubectl("get", "pods", "-n", "kube-system", "-o", "json"))["items"]
    bad = [
        p["metadata"]["name"] for p in pods if p["status"]["phase"] not in ("Running", "Succeeded")
    ]
    check(not bad, f"kube-system pods healthy {bad or ''}".strip())

    kubectl("delete", "namespace", NS, "--ignore-not-found", "--wait=true", "--timeout=120s")
    try:
        kubectl(
            "apply", "-f", "-", stdin=manifests(names, agents[0][1] if agents else None, server)
        )
        kubectl("-n", NS, "rollout", "status", "deploy/spread", "--timeout=180s")
        placed = {
            p["spec"]["nodeName"]
            for p in json.loads(kubectl("-n", NS, "get", "pods", "-l", "app=spread", "-o", "json"))[
                "items"
            ]
        }
        check(
            placed == set(names),
            f"spread test: one pod on each of {sorted(names)} (got {sorted(placed)})",
        )

        kubectl("-n", NS, "rollout", "status", "deploy/web", "--timeout=180s")
        code = None
        for _ in range(30):
            code = http_status(f"http://{server_addr}/", "verify.kube-ax.local")
            if code == 200:
                break
            time.sleep(2)
        target = agents[0][1] if agents else server
        check(code == 200, f"ingress via {server_addr} -> pod on {target}: HTTP 200 (got {code})")

        for addr, name in agents:
            check(
                http_status(f"http://{addr}/", "verify.kube-ax.local", timeout=2) is None,
                f"{name}: no LoadBalancer port 80 on agent IP {addr}",
            )
    finally:
        kubectl("delete", "namespace", NS, "--ignore-not-found", "--wait=false", check_rc=False)


def _node_mem_kib() -> dict[str, int]:
    """MemTotal of every inventory node, for comparing with kubelet's recorded capacity."""
    out = {}
    for h in inventory:
        if h.connected:
            kib = h.get_fact(Command, command="awk '/MemTotal/{print $2}' /proc/meminfo")
            out[h.data.get("node_name")] = int(kib)
    return out


data = host.data.dict()
cpu = host.get_fact(CpuCapabilities)
tuning = host.get_fact(TuningState)
loaded = host.get_fact(Command, command="systemctl show k3s -p LoadState --value").strip()
mem_kib = int(host.get_fact(Command, command="awk '/MemTotal/{print $2}' /proc/meminfo"))

disk = host.get_fact(
    Command,
    command=(
        "df -B1 --output=target,size,avail / /var/lib/rancher/k3s/agent/containerd | tail -n +2 "
        "| awk '{print $1, $2, $3, \"-\"}'; "
        'echo "cache 0 0 $(sudo -n du -sb /var/lib/ate/image-cache | cut -f1)"; '
        'echo "ate 0 0 $(findmnt -no SOURCE --target /var/lib/ate)"'
    ),
)
python.call(
    name=f"{data['node_name']}: disk space and image cache",
    function=disk_checks,
    node=data["node_name"],
    disk=disk,
    ate_on_containerd=data["ate_on_containerd"],
)

python.call(
    name=f"{data['node_name']}: CPU, swap, sysctls, k3s unit",
    function=node_checks,
    node=data["node_name"],
    cpu=cpu,
    tuning=tuning,
    swap_mode=data["swap_mode"],
    loaded=loaded,
    mem_kib=mem_kib,
    mem_gib=data["node_memory_gib"],
)

if data["role"] == "server":
    python.call(
        name="Cluster: nodes, pods, spread, ingress, LB isolation",
        function=cluster_checks,
        server_addr=host.name,
        inventory_nodes=nodes(),
        node_mem_kib=_node_mem_kib(),
    )
