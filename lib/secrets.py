"""Local secret files under secrets/ (gitignored, mode 0600). Requirement 9, 14."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "secrets"


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def k3s_token(root: Path = ROOT) -> str:
    """Return the cluster join token, generating it on first use (Req 9.1)."""
    path = root / "k3s-token"
    if not path.exists():
        _write_private(path, secrets.token_hex(32) + "\n")
    path.chmod(0o600)
    return path.read_text().strip()


def optional(name: str, root: Path = ROOT) -> str | None:
    """Contents of secrets/<name>, or None if the operator hasn't provided it."""
    path = root / name
    return path.read_text().strip() if path.exists() else None
