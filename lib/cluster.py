"""Helpers shared by deploys: inventory-wide views and node.json rendering from live facts."""

from __future__ import annotations

from typing import Any

from pyinfra import inventory

from lib import hostnet, nodeconfig
from lib.facts import CpuCapabilities, PrimaryNetwork


def nodes() -> list[tuple[str, dict[str, Any]]]:
    """Every inventory host as (address, merged data), in inventory order."""
    return [(h.name, h.data.dict()) for h in inventory]


def network_for(host) -> dict[str, Any]:
    """Committed host_network/<node>.json if present, else the live capture + overrides."""
    data = host.data.dict()
    committed = hostnet.load(data["node_name"])
    if committed is not None:
        return committed
    return hostnet.merge(host.get_fact(PrimaryNetwork), data)


def render_node_config(host, phase: str) -> dict[str, Any]:
    return nodeconfig.render(
        host.name,
        host.data.dict(),
        network_for(host),
        host.get_fact(CpuCapabilities),
        nodes(),
        phase,
    )
