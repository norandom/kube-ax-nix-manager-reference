"""Custom pyinfra facts (design: "lib/facts.py"). All are read-only."""

from __future__ import annotations

import json
import shlex
from typing import Any

from pyinfra.api import FactBase

SEP = "---8<---"

# sysctls asserted by verify (Req 13.16); values are what modules/tuning.nix sets.
TUNING_SYSCTLS = (
    "vm.swappiness",
    "vm.page-cluster",
    "vm.overcommit_memory",
    "vm.panic_on_oom",
    "kernel.panic",
    "kernel.panic_on_oops",
    "fs.inotify.max_user_instances",
    "fs.inotify.max_user_watches",
    "net.core.default_qdisc",
    "net.ipv4.tcp_congestion_control",
    "net.ipv4.ip_forward",
    "net.bridge.bridge-nf-call-iptables",
    "net.ipv4.conf.all.proxy_arp",
    "net.ipv6.conf.all.proxy_ndp",
)


def _sections(output: list[str], count: int) -> list[list[str]]:
    parts: list[list[str]] = [[]]
    for line in output:
        if line.strip() == SEP:
            parts.append([])
        else:
            parts[-1].append(line)
    return parts + [[] for _ in range(count - len(parts))]


class IsLiveIso(FactBase[bool]):
    """True when booted from the NixOS installer ISO (squashfs store at /nix/.ro-store)."""

    def command(self) -> str:
        return "findmnt -n /nix/.ro-store >/dev/null 2>&1 && echo yes || echo no"

    def process(self, output: list[str]) -> bool:
        return output[-1].strip() == "yes"


class BootMode(FactBase[str]):
    def command(self) -> str:
        return "test -d /sys/firmware/efi && echo uefi || echo bios"

    def process(self, output: list[str]) -> str:
        return output[-1].strip()


class DiskState(FactBase[dict]):
    """Whether ``disk`` is blank: no partitions, filesystem, partition table, or signatures.

    Needs root for ``wipefs -n`` (call with ``_sudo=True``).
    """

    def command(self, disk: str) -> str:
        d = shlex.quote(disk)
        return (
            f"if ! test -b {d}; then echo '{{\"missing\": true}}'; exit 0; fi; "
            f"lsblk -J -b -o NAME,TYPE,FSTYPE,PTTYPE,SIZE {d}; echo '{SEP}'; "
            f"wipefs -n {d} 2>/dev/null || true"
        )

    def process(self, output: list[str]) -> dict[str, Any]:
        lsblk, wipefs = _sections(output, 2)
        info = json.loads("\n".join(lsblk))
        if info.get("missing"):
            return {"exists": False, "empty": False, "detail": "device does not exist"}
        dev = info["blockdevices"][0]
        signatures = [line for line in wipefs if line.strip()][1:]  # drop header row
        reasons = []
        if dev.get("children"):
            reasons.append(f"partitions: {', '.join(c['name'] for c in dev['children'])}")
        if dev.get("pttype"):
            reasons.append(f"partition table: {dev['pttype']}")
        if dev.get("fstype"):
            reasons.append(f"filesystem: {dev['fstype']}")
        if signatures:
            reasons.append(f"{len(signatures)} wipefs signature(s)")
        return {
            "exists": True,
            "empty": not reasons,
            "size_bytes": int(dev["size"]),
            "detail": "; ".join(reasons) or "blank",
        }


class PrimaryNetwork(FactBase[dict]):
    """Interface, MAC, address/prefix, gateway and DNS of the default route (Req 4.1)."""

    def command(self) -> str:
        return (
            f"ip -j route show default; echo '{SEP}'; ip -j addr show; echo '{SEP}'; "
            f"(resolvectl dns 2>/dev/null || true); echo '{SEP}'; "
            "cat /etc/resolv.conf 2>/dev/null || true"
        )

    def process(self, output: list[str]) -> dict[str, Any]:
        routes, addrs, resolvectl, resolv_conf = _sections(output, 4)
        default = json.loads("\n".join(routes) or "[]")
        if not default:
            return {}
        route = default[0]
        iface = route["dev"]
        link = next(a for a in json.loads("\n".join(addrs)) if a["ifname"] == iface)
        inet = next(
            a for a in link.get("addr_info", []) if a["family"] == "inet" and a["scope"] == "global"
        )
        return {
            "iface": iface,
            "mac": link["address"],
            "address": inet["local"],
            "prefix": inet["prefixlen"],
            "gateway": route.get("gateway"),
            "dns": self._dns(iface, resolvectl, resolv_conf),
            "dhcp": route.get("protocol") == "dhcp" or bool(inet.get("dynamic")),
        }

    @staticmethod
    def _dns(iface: str, resolvectl: list[str], resolv_conf: list[str]) -> list[str]:
        # resolvectl lines look like "Link 2 (ens18): 10.0.0.1 1.1.1.1"
        for line in resolvectl:
            if f"({iface})" in line and ":" in line:
                servers = line.split(":", 1)[1].split()
                if servers:
                    return servers
        stub = {"127.0.0.53", "127.0.0.54"}
        return [
            parts[1]
            for parts in (line.split() for line in resolv_conf)
            if len(parts) >= 2 and parts[0] == "nameserver" and parts[1] not in stub
        ]


class CpuCapabilities(FactBase[dict]):
    """SIMD flags and KVM availability (Req 3.13, 16.6)."""

    def command(self) -> str:
        return (
            "grep -m1 '^flags' /proc/cpuinfo; grep -m1 '^model name' /proc/cpuinfo; "
            "test -c /dev/kvm && echo devkvm=yes || echo devkvm=no; nproc"
        )

    def process(self, output: list[str]) -> dict[str, Any]:
        flags: set[str] = set()
        model = ""
        dev_kvm = False
        cpus = 0
        for line in output:
            if line.startswith("flags"):
                flags = set(line.split(":", 1)[1].split())
            elif line.startswith("model name"):
                model = line.split(":", 1)[1].strip()
            elif line.startswith("devkvm="):
                dev_kvm = line.strip() == "devkvm=yes"
            elif line.strip().isdigit():
                cpus = int(line)
        kvm = "intel" if "vmx" in flags else "amd" if "svm" in flags else None
        return {
            "model": model,
            "cpus": cpus,
            "avx2": "avx2" in flags,
            "fma": "fma" in flags,
            "avx512f": "avx512f" in flags,
            "kvm": kvm,
            "dev_kvm": dev_kvm,
        }


class K3sNodeReady(FactBase[bool]):
    """Whether ``node_name`` reports Ready, asked from the server (needs ``_sudo=True``)."""

    def command(self, node_name: str) -> str:
        jsonpath = shlex.quote('{.status.conditions[?(@.type=="Ready")].status}')
        return (
            f"k3s kubectl get node {shlex.quote(node_name)} -o jsonpath={jsonpath} 2>/dev/null"
            " || echo missing"
        )

    def process(self, output: list[str]) -> bool:
        return bool(output) and output[-1].strip() == "True"


class TuningState(FactBase[dict]):
    """Swap devices, key sysctls and the k3s unit's OOMScoreAdjust (Req 13.16)."""

    def command(self) -> str:
        keys = " ".join(TUNING_SYSCTLS)
        return (
            f"swapon --show=NAME,TYPE,SIZE --noheadings --bytes; echo '{SEP}'; "
            f"sysctl -e {keys}; echo '{SEP}'; "
            "systemctl show k3s -p OOMScoreAdjust --value 2>/dev/null || true"
        )

    def process(self, output: list[str]) -> dict[str, Any]:
        swaps, sysctls, oom = _sections(output, 3)
        swap = []
        for line in swaps:
            parts = line.split()
            if len(parts) == 3:
                swap.append({"name": parts[0], "type": parts[1], "size_bytes": int(parts[2])})
        values = {}
        for line in sysctls:
            if "=" in line:
                key, value = (p.strip() for p in line.split("=", 1))
                values[key] = value
        oom_value = next((line.strip() for line in oom if line.strip()), None)
        return {"swap": swap, "sysctl": values, "k3s_oom_score_adjust": oom_value}
