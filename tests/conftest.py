import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _group(name: str) -> dict:
    return {
        k: v
        for k, v in runpy.run_path(str(ROOT / "group_data" / f"{name}.py")).items()
        if not k.startswith("__") and k != "Path"
    }


@pytest.fixture
def all_data() -> dict:
    data = _group("all")
    data["admin_ssh_keys"] = ["ssh-ed25519 AAAATEST test@example"]
    return data


@pytest.fixture
def server(all_data) -> tuple[str, dict]:
    data = (
        all_data
        | _group("servers")
        | {"node_name": "kube-01", "disk": "/dev/sda", "boot_mode": "bios"}
    )
    return "10.0.0.63", data


@pytest.fixture
def agent(all_data) -> tuple[str, dict]:
    data = (
        all_data
        | _group("agents")
        | {"node_name": "kube-02", "disk": "/dev/sda", "boot_mode": "uefi"}
    )
    return "10.0.0.64", data


@pytest.fixture
def cpu() -> dict:
    return {"avx2": True, "fma": True, "avx512f": False, "kvm": "intel", "dev_kvm": True}


def network_for(address: str) -> dict:
    return {
        "iface": "ens18",
        "mac": "bc:24:11:a3:c7:ba",
        "address": address,
        "prefix": 24,
        "gateway": "10.0.0.1",
        "dns": ["10.0.0.1"],
    }
