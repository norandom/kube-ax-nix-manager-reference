"""Render the per-node ``node.json`` consumed by the Nix flake (design: "node.json (schema 1)").

Pure functions over plain dicts so they can be unit-tested without pyinfra. Inputs:

- ``data``: the host's merged inventory + group data (``host.data.dict()`` in deploys)
- ``network``: captured network identity (``host_network/<node>.json``), see ``lib.hostnet``
- ``cpu``: the ``CpuCapabilities`` fact
- ``nodes``: every node in the inventory as ``(address, data)``, used for cluster-wide decisions
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Iterable, Mapping
from typing import Any

SCHEMA = 1
PHASES = ("base", "k3s")
TOKEN_FILE = "/var/lib/k3s-secrets/token"
ENTRYPOINT_LABEL = "svccontroller.k3s.cattle.io/enablelb"
ZONE_LABEL = "topology.kubernetes.io/zone"

REQUIRED_HOST_KEYS = ("node_name", "disk", "boot_mode", "role")
REQUIRED_NETWORK_KEYS = ("mac", "address", "prefix", "gateway", "dns")
LVM_VOLUMES = ("root", "containerd", "storage")


class NodeConfigError(ValueError):
    """Inventory or fact data is incomplete or inconsistent for a host."""

    def __init__(self, host: str, message: str):
        super().__init__(f"{host}: {message}")
        self.host = host


def _require(host: str, mapping: Mapping[str, Any], keys: Iterable[str], where: str) -> None:
    missing = [k for k in keys if mapping.get(k) in (None, "", [])]
    if missing:
        raise NodeConfigError(host, f"missing {where} field(s): {', '.join(missing)}")


def find_server(nodes: Iterable[tuple[str, Mapping[str, Any]]]) -> tuple[str, Mapping[str, Any]]:
    """Return the single server node; the initial version supports exactly one (Req 2.2)."""
    servers = [(addr, data) for addr, data in nodes if data.get("role") == "server"]
    if len(servers) != 1:
        names = ", ".join(addr for addr, _ in servers) or "none"
        raise NodeConfigError(
            "inventory", f"exactly one server required, found {len(servers)}: {names}"
        )
    return servers[0]


def flannel_backend(setting: str, server_cidr: str, addresses: Iterable[str]) -> str:
    """``auto`` -> ``host-gw`` when every node is inside the server's subnet, else ``vxlan``."""
    if setting != "auto":
        return setting
    subnet = ipaddress.ip_interface(server_cidr).network
    same_l2 = all(ipaddress.ip_address(a) in subnet for a in addresses)
    return "host-gw" if same_l2 else "vxlan"


def _kvm_vendor(host: str, cpu: Mapping[str, Any]) -> str:
    # Micro-VM sandboxes and vectorised numpy/JVM code are mandatory (Req 3.13, 16.6).
    missing = [flag for flag in ("avx2", "fma") if not cpu.get(flag)]
    if missing:
        raise NodeConfigError(
            host, f"CPU lacks {', '.join(missing)}: set the Proxmox CPU type to 'host'"
        )
    vendor = cpu.get("kvm")
    if vendor not in ("intel", "amd") or not cpu.get("dev_kvm"):
        raise NodeConfigError(
            host, "no vmx/svm or /dev/kvm: enable nested virtualisation and CPU type 'host'"
        )
    return vendor


def _disk_layout(data: Mapping[str, Any], host: str) -> dict[str, str]:
    """LVM logical volume sizes (e.g. "20G"); unallocated space stays free for lvextend."""
    layout = dict(data.get("disk_layout") or {})
    missing = [lv for lv in LVM_VOLUMES if not layout.get(lv)]
    if missing:
        raise NodeConfigError(host, f"disk_layout missing size for: {', '.join(missing)}")
    for lv, size in layout.items():
        if lv not in LVM_VOLUMES:
            raise NodeConfigError(host, f"disk_layout has unknown volume {lv!r}")
        if not (size[:-1].isdigit() and size[-1] in "MGT"):
            raise NodeConfigError(host, f"disk_layout[{lv}] must look like 20G, got {size!r}")
    return layout


def _kubelet(data: Mapping[str, Any], host: str) -> dict[str, Any]:
    policy = data["cpu_manager_policy"]
    if policy not in ("none", "static"):
        raise NodeConfigError(host, f"cpu_manager_policy must be none|static, got {policy!r}")
    if policy == "static" and not data.get("reserved_cpus"):
        raise NodeConfigError(host, "cpu_manager_policy=static requires reserved_cpus")
    if data["swap_behavior"] not in ("NoSwap", "LimitedSwap"):
        raise NodeConfigError(
            host, f"swap_behavior must be NoSwap|LimitedSwap, got {data['swap_behavior']!r}"
        )
    return {
        "systemReserved": dict(data["system_reserved"]),
        "kubeReserved": dict(data["kube_reserved"]),
        "evictionHard": dict(data["eviction_hard"]),
        "evictionSoft": dict(data["eviction_soft"]),
        "evictionSoftGracePeriod": dict(data["eviction_soft_grace"]),
        "swapBehavior": data["swap_behavior"],
        "kernelMemcgNotification": True,
        "cpuManagerPolicy": policy,
        "reservedCpus": data.get("reserved_cpus"),
        "imageGcHigh": int(data["image_gc_high"]),
        "imageGcLow": int(data["image_gc_low"]),
    }


def _tuning(data: Mapping[str, Any], host: str) -> dict[str, Any]:
    if data["swap_mode"] not in ("zram", "zswap", "none"):
        raise NodeConfigError(host, f"swap_mode must be zram|zswap|none, got {data['swap_mode']!r}")
    if data["cpu_mitigations"] not in ("auto", "off"):
        raise NodeConfigError(
            host, f"cpu_mitigations must be auto|off, got {data['cpu_mitigations']!r}"
        )
    return {
        "swapMode": data["swap_mode"],
        "zramPercent": int(data["zram_percent"]),
        "zramAlgorithm": data["zram_algorithm"],
        "zswapSwapfileGb": int(data["zswap_swapfile_gb"]),
        "ksm": bool(data["ksm"]),
        "kernelPreempt": data["kernel_preempt"],
        "cpuMitigations": data["cpu_mitigations"],
        "cpuidleHaltpoll": bool(data["cpuidle_haltpoll"]),
        "softLockupWatchdog": bool(data["soft_lockup_watchdog"]),
        "nofile": {"soft": int(data["nofile_soft"]), "hard": int(data["nofile_hard"])},
        "sysctl": dict(data.get("sysctl_overrides") or {}),
    }


def render(
    address: str,
    data: Mapping[str, Any],
    network: Mapping[str, Any],
    cpu: Mapping[str, Any],
    nodes: list[tuple[str, Mapping[str, Any]]],
    phase: str,
) -> dict[str, Any]:
    """Build the node.json dict for one host. Raises NodeConfigError before anything is changed."""
    host = data.get("node_name") or address
    if phase not in PHASES:
        raise NodeConfigError(host, f"phase must be one of {PHASES}, got {phase!r}")
    _require(host, data, REQUIRED_HOST_KEYS, "inventory")
    _require(host, network, REQUIRED_NETWORK_KEYS, "network")
    if not data.get("admin_ssh_keys"):
        raise NodeConfigError(
            host, "admin_ssh_keys is empty: the installed node would be unreachable"
        )
    if data["boot_mode"] not in ("bios", "uefi"):
        raise NodeConfigError(host, f"boot_mode must be bios|uefi, got {data['boot_mode']!r}")
    if data["role"] not in ("server", "agent"):
        raise NodeConfigError(host, f"role must be server|agent, got {data['role']!r}")
    if network["address"] != address:
        raise NodeConfigError(
            host, f"captured address {network['address']} != inventory address {address}"
        )

    server_addr, server_data = find_server(nodes)
    is_server = data["role"] == "server"
    cidr = f"{network['address']}/{network['prefix']}"

    labels = {ZONE_LABEL: data["node_name"], **dict(data.get("node_labels") or {})}
    k3s: dict[str, Any] = {
        "role": data["role"],
        "nodeIp": address,
        "tokenFile": TOKEN_FILE,
        "labels": labels,
        "kubelet": _kubelet(data, host),
        "ateOnContainerd": bool(data["ate_on_containerd"]),
        # Cluster-wide: every node needs it (agents open UDP 8472 for vxlan too). Decided from the
        # server's setting and subnet, assuming nodes on one L2 share the prefix.
        "flannelBackend": flannel_backend(
            server_data["k3s_flannel_backend"],
            f"{server_addr}/{network['prefix']}",
            [addr for addr, _ in nodes],
        ),
    }
    if is_server:
        labels[ENTRYPOINT_LABEL] = "true"
        k3s |= {
            "serverAddr": None,
            "entryPoint": True,
            "disable": list(data["k3s_disable"]),
            "traefikReplicas": int(data["traefik_replicas"]),
        }
    else:
        k3s |= {"serverAddr": f"https://{server_addr}:6443", "entryPoint": False}

    return {
        "schema": SCHEMA,
        "phase": phase,
        "hostName": data["node_name"],
        "disk": data["disk"],
        "diskLayout": _disk_layout(data, host),
        "bootMode": data["boot_mode"],
        "kvm": _kvm_vendor(host, cpu),
        "network": {
            "mac": network["mac"],
            "address": cidr,
            "gateway": network["gateway"],
            "dns": list(network["dns"]),
        },
        "admin": {"user": data["admin_user"], "sshKeys": list(data["admin_ssh_keys"])},
        "tuning": _tuning(data, host),
        "k3s": k3s,
    }


def dumps(config: Mapping[str, Any]) -> str:
    """Stable serialisation so unchanged config uploads as an unchanged file (Req 10.1)."""
    return json.dumps(config, indent=2, sort_keys=True) + "\n"
