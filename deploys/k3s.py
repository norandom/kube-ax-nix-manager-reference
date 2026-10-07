"""Phase 2: enable k3s on installed nodes via nixos-rebuild (Requirements 5, 6, 9, 10, 14).

    just k3s [HOST]   # servers first, then agents one at a time

Runs as the admin user (ops). Re-running without changes rebuilds nothing and restarts nothing.
"""

from __future__ import annotations

import json
import subprocess
import time
from io import StringIO

from pyinfra import host, logger
from pyinfra.operations import files, python, server
from pyinfra.operations.util import any_changed

from lib import nodeconfig, secrets
from lib.cluster import nodes, render_node_config
from lib.facts import IsLiveIso

TOKEN_DIR = "/var/lib/k3s-secrets"
READY_TIMEOUT = 300


class K3sError(RuntimeError):
    pass


def wait_until_ready(server_addr: str, admin_user: str, node_name: str) -> None:
    """Ask the server (over SSH) until node_name is Ready (Req 5.5, 6.3)."""
    jsonpath = '{.status.conditions[?(@.type=="Ready")].status}'
    cmd = f"sudo k3s kubectl get node {node_name} -o jsonpath='{jsonpath}'"
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", f"{admin_user}@{server_addr}", cmd],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.stdout.strip() == "True":
            logger.info("%s: Ready", node_name)
            return
        time.sleep(5)
    raise K3sError(
        f"{node_name} not Ready after {READY_TIMEOUT}s; see `journalctl -u k3s -n 100` on the node"
    )


def registries_yaml(owner: str, token: str) -> str:
    # JSON is valid YAML; credentials stay out of the Nix store (Req 14.2).
    return json.dumps({"configs": {"ghcr.io": {"auth": {"username": owner, "password": token}}}})


def ghcr_owner() -> str | None:
    owner = host.data.get("ghcr_owner")
    if owner:
        return owner
    out = subprocess.run(
        ["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True, check=False
    )
    return out.stdout.strip() or None


if host.get_fact(IsLiveIso):
    raise K3sError(f"{host.name}: still on the live ISO; run `just install {host.name}` first")

data = host.data.dict()
server_addr, _ = nodeconfig.find_server(nodes())
config = render_node_config(host, "k3s")

token = files.put(
    name="k3s cluster token (outside the Nix store)",
    src=StringIO(secrets.k3s_token() + "\n"),
    dest=f"{TOKEN_DIR}/token",
    user="root",
    group="root",
    mode="600",
    create_remote_dir=True,
    _sudo=True,
)
files.directory(name="Token directory 0700", path=TOKEN_DIR, mode="700", _sudo=True)

ghcr_token = secrets.optional("ghcr-token")
if ghcr_token:
    owner = ghcr_owner()
    if not owner:
        raise K3sError("secrets/ghcr-token exists but ghcr_owner is unset and `gh` isn't logged in")
    registries = files.put(
        name="ghcr.io pull credentials (registries.yaml)",
        src=StringIO(registries_yaml(owner, ghcr_token)),
        dest="/etc/rancher/k3s/registries.yaml",
        user="root",
        group="root",
        mode="600",
        create_remote_dir=True,
        _sudo=True,
    )
else:
    logger.warning("%s: no secrets/ghcr-token, only public images can be pulled", host.name)
    registries = None

flake = files.sync(
    name="Sync flake to /etc/nixos (hardware-configuration.nix stays)",
    src="nix/",
    dest="/etc/nixos",
    exclude=["result*", "node.json", "hardware-configuration.nix"],
    _sudo=True,
)
node_json = files.put(
    name="node.json (phase=k3s)",
    src=StringIO(nodeconfig.dumps(config)),
    dest="/etc/nixos/node.json",
    _sudo=True,
)

rebuild = server.shell(
    name="nixos-rebuild switch",
    commands=["nixos-rebuild switch --flake path:/etc/nixos#node"],
    _sudo=True,
    _if=any_changed(flake, node_json, token),
)

if registries is not None:
    server.shell(
        name="Restart k3s to load registry credentials",
        commands=["systemctl restart k3s"],
        _sudo=True,
        _if=registries.did_change,
    )

python.call(
    name=f"Wait for {data['node_name']} Ready",
    function=wait_until_ready,
    server_addr=server_addr,
    admin_user=data["admin_user"],
    node_name=data["node_name"],
)
