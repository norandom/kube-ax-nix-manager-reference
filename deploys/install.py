"""Phase 1: live NixOS ISO -> installed base system (Requirement 3, 4).

    just install HOST [WIPE=/dev/sda]
    (= uv run pyinfra inventory.py deploys/install.py --limit HOST --data ssh_user=nixos
       [--data wipe_disk=/dev/sda])

Preconditions are checked before anything changes; an already-installed host is skipped.
Ends with a reboot into the installed system and waits until `ops@<ip>` answers over SSH.
"""

from __future__ import annotations

import socket
import subprocess
import time
from io import StringIO

from pyinfra import host, logger
from pyinfra.operations import files, python, server

from lib import hostnet, nodeconfig
from lib.cluster import nodes
from lib.facts import BootMode, CpuCapabilities, DiskState, IsLiveIso, PrimaryNetwork

STAGE = "/tmp/kube-nix"
NIX_ENV = 'export NIX_CONFIG="experimental-features = nix-command flakes"'
SSH_TIMEOUT = 600


class InstallError(RuntimeError):
    pass


def wait_for_installed_system(address: str, user: str) -> None:
    """Runs locally after the reboot command: new host key, then SSH as the admin user."""
    subprocess.run(["ssh-keygen", "-R", address], capture_output=True, check=False)
    # Let the ISO go down first so we don't mistake its sshd for the installed one.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            socket.create_connection((address, 22), timeout=2).close()
            time.sleep(2)
        except OSError:
            break
    deadline = time.monotonic() + SSH_TIMEOUT
    while time.monotonic() < deadline:
        probe = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "ConnectTimeout=5",
                f"{user}@{address}",
                "true",
            ],
            capture_output=True,
            check=False,
        )
        if probe.returncode == 0:
            logger.info("%s: installed system reachable as %s", address, user)
            return
        time.sleep(5)
    raise InstallError(
        f"{address}: no SSH as {user} within {SSH_TIMEOUT}s after reboot. Check the VM console; "
        "boot order must be disk before CD-ROM."
    )


def preflight() -> dict | None:
    """Return the base-phase node.json, None to skip this host, or raise before any change."""
    data = host.data.dict()
    name = data.get("node_name", host.name)

    if not host.get_fact(IsLiveIso):
        logger.warning("%s: not running the NixOS live ISO, assuming installed; skipping", name)
        return None

    boot_mode = host.get_fact(BootMode)
    if boot_mode != data["boot_mode"]:
        raise InstallError(
            f"{name}: firmware boots {boot_mode}, inventory says {data['boot_mode']}"
        )

    disk = data["disk"]
    state = host.get_fact(DiskState, disk, _sudo=True)
    if not state["exists"]:
        raise InstallError(f"{name}: install disk {disk} does not exist")
    if not state["empty"] and data.get("wipe_disk") != disk:
        raise InstallError(
            f"{name}: {disk} is not blank ({state['detail']}). "
            f"Re-run with WIPE={disk} to destroy its contents."
        )

    captured = hostnet.merge(host.get_fact(PrimaryNetwork), data)
    committed = hostnet.load(name)
    if committed and committed != captured and not data.get("recapture_network"):
        raise InstallError(
            f"{name}: network now {captured}, committed {committed}. "
            "Pass --data recapture_network=True if that is intended."
        )

    # Also validates CPU (AVX2/FMA/vmx, /dev/kvm) and that the lease matches the inventory IP.
    config = nodeconfig.render(
        host.name, data, captured, host.get_fact(CpuCapabilities), nodes(), "base"
    )
    return {"config": config, "network": captured, "name": name, "disk": disk, "state": state}


plan = preflight()

if plan is not None:
    python.call(
        name=f"Record network identity in host_network/{plan['name']}.json",
        function=hostnet.save,
        node_name=plan["name"],
        network=plan["network"],
        recapture=bool(host.data.get("recapture_network")),
    )

    files.sync(
        name="Upload flake to the ISO",
        src="nix/",
        dest=STAGE,
        delete=True,
        exclude=["result*", "node.json", "hardware-configuration.nix"],
    )
    files.put(
        name="Upload node.json (phase=base)",
        src=StringIO(nodeconfig.dumps(plan["config"])),
        dest=f"{STAGE}/node.json",
    )

    wipe = "wipe" if not plan["state"]["empty"] else "blank disk"
    server.shell(
        name=f"disko: partition, format and mount {plan['disk']} ({wipe})",
        commands=[
            f"{NIX_ENV}; nix run path:{STAGE}#disko -- --mode destroy,format,mount "
            f"--yes-wipe-all-disks --flake path:{STAGE}#node",
            "findmnt /mnt >/dev/null",
        ],
        _sudo=True,
    )

    server.shell(
        name="Generate hardware config and place the flake in /mnt/etc/nixos",
        commands=[
            "nixos-generate-config --no-filesystems --root /mnt",
            f"cp -rT {STAGE} /mnt/etc/nixos",
            "rm -f /mnt/etc/nixos/configuration.nix",
        ],
        _sudo=True,
    )

    server.shell(
        name="nixos-install (base system, no root password)",
        commands=[
            f"{NIX_ENV}; nixos-install --flake path:/mnt/etc/nixos#node "
            "--no-root-passwd --no-channel-copy"
        ],
        _sudo=True,
    )

    server.shell(
        name="Reboot into the installed system",
        commands=["systemd-run --on-active=3 systemctl reboot"],
        _sudo=True,
    )

    python.call(
        name="Wait for the installed system over SSH",
        function=wait_for_installed_system,
        address=host.name,
        user=host.data.get("admin_user"),
    )
