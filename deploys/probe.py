"""Read-only probe: print every custom fact and the node.json this host would get.

    uv run pyinfra inventory.py deploys/probe.py --limit 10.0.0.63 --data ssh_user=nixos

Changes nothing on the host and writes nothing locally.
"""

import json

from pyinfra import host, logger

from lib import nodeconfig
from lib.cluster import network_for, render_node_config
from lib.facts import (
    BootMode,
    CpuCapabilities,
    DiskState,
    IsLiveIso,
    PrimaryNetwork,
    TuningState,
)

data = host.data.dict()
report = {
    "is_live_iso": host.get_fact(IsLiveIso),
    "boot_mode": host.get_fact(BootMode),
    "disk": host.get_fact(DiskState, data["disk"], _sudo=True),
    "primary_network": host.get_fact(PrimaryNetwork),
    "network_used": network_for(host),
    "cpu": host.get_fact(CpuCapabilities),
    "tuning": host.get_fact(TuningState),
}
try:
    report["node_json_base"] = render_node_config(host, "base")
except nodeconfig.NodeConfigError as exc:
    report["node_json_error"] = str(exc)

logger.info("probe %s:\n%s", host.name, json.dumps(report, indent=2, sort_keys=True))
