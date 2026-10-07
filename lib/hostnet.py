"""Captured network identity per node: ``host_network/<node_name>.json`` (Requirement 4).

The install phase captures the DHCP lease from the live ISO (``PrimaryNetwork`` fact), applies
inventory overrides, and commits the result. The installed system configures it statically, and
later runs reuse the file so they never depend on the ISO again.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent / "host_network"
KEYS = ("iface", "mac", "address", "prefix", "gateway", "dns")


class NetworkChangedError(RuntimeError):
    pass


def path_for(node_name: str, root: Path = ROOT) -> Path:
    return root / f"{node_name}.json"


def merge(captured: Mapping[str, Any], data: Mapping[str, Any]) -> dict[str, Any]:
    """Captured lease values; inventory overrides (ip_prefix, gateway, dns) take precedence."""
    net = {k: captured.get(k) for k in KEYS}
    if data.get("ip_prefix") is not None:
        net["prefix"] = int(data["ip_prefix"])
    if data.get("gateway"):
        net["gateway"] = data["gateway"]
    if data.get("dns"):
        dns = data["dns"]
        net["dns"] = [dns] if isinstance(dns, str) else list(dns)
    return net


def load(node_name: str, root: Path = ROOT) -> dict[str, Any] | None:
    p = path_for(node_name, root)
    return json.loads(p.read_text()) if p.exists() else None


def save(
    node_name: str, network: Mapping[str, Any], *, recapture: bool = False, root: Path = ROOT
) -> bool:
    """Persist network identity. Returns True if the file was written.

    Refuses to silently replace a committed identity with different values (Req 4.3); pass
    ``recapture=True`` (``--data recapture_network=True``) to accept the new lease deliberately.
    """
    new = {k: network.get(k) for k in KEYS}
    old = load(node_name, root)
    if old == new:
        return False
    if old is not None and not recapture:
        diff = {k: (old.get(k), new.get(k)) for k in KEYS if old.get(k) != new.get(k)}
        raise NetworkChangedError(
            f"{node_name}: captured network differs from {path_for(node_name, root)}: {diff}. "
            "Re-run with --data recapture_network=True if this is intended."
        )
    root.mkdir(parents=True, exist_ok=True)
    path_for(node_name, root).write_text(json.dumps(new, indent=2, sort_keys=True) + "\n")
    return True
