"""Regenerate nix/fixtures/*.json from lib.nodeconfig so Nix checks test real renderer output.

    uv run python scripts/gen_fixtures.py

tests/test_fixtures.py fails if the committed fixtures are stale.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import nodeconfig  # noqa: E402

FIXTURES = ROOT / "nix" / "fixtures"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureKeyFixtureKeyFixtureKeyFixtureKey fixture"
INTEL = {"avx2": True, "fma": True, "avx512f": False, "kvm": "intel", "dev_kvm": True}
AMD = INTEL | {"kvm": "amd"}


def group(name: str) -> dict:
    data = runpy.run_path(str(ROOT / "group_data" / f"{name}.py"))
    return {k: v for k, v in data.items() if not k.startswith("__") and k != "Path"}


def network(address: str) -> dict:
    return {
        "iface": "ens18",
        "mac": "bc:24:11:00:00:01",
        "address": address,
        "prefix": 24,
        "gateway": "10.0.0.1",
        "dns": ["10.0.0.1"],
    }


def build() -> dict[str, str]:
    base = group("all") | {"admin_ssh_keys": [KEY]}
    server = (
        "10.0.0.63",
        base | group("servers") | {"node_name": "kube-01", "disk": "/dev/sda", "boot_mode": "bios"},
    )
    agent = (
        "10.0.0.64",
        base
        | group("agents")
        | {"node_name": "kube-02", "disk": "/dev/vda", "boot_mode": "uefi", "swap_mode": "zswap"},
    )
    tuned = (
        "10.0.0.63",
        server[1]
        | {
            "cpu_manager_policy": "static",
            "reserved_cpus": "0",
            "swap_behavior": "LimitedSwap",
            "ksm": True,
            "cpu_mitigations": "off",
            "cpuidle_haltpoll": True,
            "soft_lockup_watchdog": True,
            "swap_mode": "none",
            "k3s_disable": ["traefik"],
            "sysctl_overrides": {"vm.swappiness": 60},
        },
    )
    cases = {
        "server-bios-zram": (server, [server, agent], INTEL, "k3s"),
        "server-bios-base": (server, [server, agent], INTEL, "base"),
        "agent-uefi-zswap": (agent, [server, agent], AMD, "k3s"),
        "server-static-cpu": (tuned, [tuned], INTEL, "k3s"),
    }
    out = {}
    for name, ((addr, data), nodes, cpu, phase) in cases.items():
        cfg = nodeconfig.render(addr, data, network(addr), cpu, nodes, phase)
        out[f"{name}.json"] = nodeconfig.dumps(cfg)
    return out


if __name__ == "__main__":
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, text in build().items():
        (FIXTURES / name).write_text(text)
        print(f"wrote nix/fixtures/{name}")
