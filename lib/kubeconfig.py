"""Rewrite k3s.yaml for remote use and store it locally, never in git (Requirement 9.3-9.6)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME_KEYS = re.compile(r"^(\s*-?\s*(?:name|cluster|user|current-context):\s*)default\s*$")


def rewrite(k3s_yaml: str, server_addr: str, cluster: str) -> str:
    """Point at the server IP and rename the `default` cluster/user/context to `cluster`."""
    out = []
    for line in k3s_yaml.splitlines():
        line = line.replace("https://127.0.0.1:6443", f"https://{server_addr}:6443")
        line = NAME_KEYS.sub(lambda m: m.group(1) + cluster, line)
        out.append(line)
    return "\n".join(out) + "\n"


def save(content: str, cluster: str, root: Path = ROOT) -> str:
    """Write kubeconfig/<cluster>.yaml (0600) if it differs.

    The directory is gitignored: the repo is public, and these files carry cluster credentials.
    """
    path = root / "kubeconfig" / f"{cluster}.yaml"
    changed = not (path.exists() and path.read_text() == content)
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    path.chmod(0o600)
    return "written" if changed else "unchanged"
