#!/usr/bin/env python3
"""End-to-end egress test: one AX task per name prefix, each probing real destinations.

    uv run python scripts/egress-test.py open      # expectations of egress-routes/open.json
    uv run python scripts/egress-test.py scoped    # expectations of egress-routes/scoped.json

A probe counts as OK only if a full TLS handshake (with certificate check) or an HTTP response
comes back, so a gateway that accepts the TCP connection and then drops it counts as BLOCKED.
Uses the restricted ax-deployer kubeconfig for tasks and the admin one for logs.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE = (
    "ghcr.io/norandom/ax-task-runner@sha256:"
    "c1c4951ee672ef77f845b2742124f6106b300e433426fde1768381ad5d55fad7"
)
# name -> (kind, host, port)
PROBES = {
    "files": ("tls", "files.because-security.com", 443),
    "github": ("tls", "github.com", 443),
    "pypi": ("tls", "pypi.org", 443),
    "lan-rustfs": ("http", "10.0.0.54", 9000),
    "plain-http": ("http", "example.com", 80),
}
PROBE_CODE = r"""
import socket, ssl, sys, time
time.sleep(4)  # the watcher attaches the policy ~1 s after start; "no policy" is cached 2 s
probes = %r
for name, (kind, host, port) in probes.items():
    try:
        s = socket.create_connection((host, port), timeout=8)
        s.settimeout(8)
        if kind == "tls":
            c = ssl.create_default_context().wrap_socket(s, server_hostname=host)
            c.close()
        else:
            s.sendall(b"GET / HTTP/1.0\r\nHost: " + host.encode() + b"\r\n\r\n")
            reply = s.recv(300)
            # The egress gateway answers refused plain HTTP itself: "403 ... egress denied".
            if not reply.startswith(b"HTTP/") or b"egress denied" in reply:
                raise OSError("no HTTP response from the destination")
            s.close()
        r = "OK"
    except Exception:
        r = "BLOCKED"
    print("EGRESS", name, r, flush=True)
print("EGRESS done", flush=True)
"""
ALL = {"files", "github", "pypi"}
# prefix -> probes that must be OK (everything else must be BLOCKED)
EXPECT = {
    "open": {
        "ousast-engine-search-exec-": ALL,
        "ousast-engine-search-verify-": set(),
        "ousast-engine-pass-": ALL,
        "egtest-": ALL,
        # test-only routes: prove host-name enforcement without touching live prefixes
        "egtest-files-": {"files"},
        "egtest-replay-": {"files", "github"},
    },
    "scoped": {
        "ousast-engine-search-exec-": ALL,
        "ousast-engine-search-verify-": {"files"},
        "ousast-engine-search-probe-": {"files"},
        "ousast-engine-pass-": {"files"},
        "ousast-engine-replay-": {"files", "github"},
        "ousast-engine-unlisted-": set(),  # deny by default inside the namespace
        "egtest-": ALL,
        "egtest-files-": {"files"},
        "egtest-replay-": {"files", "github"},
    },
}


def run(cmd: list[str], kubeconfig: str, stdin: str | None = None) -> str:
    env = dict(os.environ, KUBECONFIG=str(ROOT / "kubeconfig" / kubeconfig))
    env["PATH"] = f"{ROOT / '.cache' / 'bin'}:{env['PATH']}"
    return subprocess.run(
        cmd, input=stdin, env=env, check=True, capture_output=True, text=True, timeout=120
    ).stdout


def probe(name: str) -> dict[str, str]:
    # base64: AX's task runner re-parses the task as YAML and trips over the program's text.
    code = base64.b64encode((PROBE_CODE % (PROBES,)).encode()).decode()
    program = f"import base64;exec(base64.b64decode('{code}'))"
    manifest = json.dumps(  # JSON is valid YAML and quotes the multi-line program safely
        {
            "apiVersion": "ax.io/v1alpha1",
            "kind": "Task",
            "metadata": {"name": name, "atespace": "default"},
            "spec": {"image": IMAGE, "command": ["python3", "-c", program]},
        }
    )
    for _ in range(30):  # one task per worker: retry while both are busy
        try:
            run(["ax", "apply", "-f", "-"], "ax-deployer.yaml", manifest)
            break
        except subprocess.CalledProcessError as e:
            if "no free workers" not in (e.stderr or "") + (e.stdout or ""):
                raise
            time.sleep(5)
    try:
        for _ in range(40):
            time.sleep(5)
            out = subprocess.run(
                [str(ROOT / "scripts" / "ax-logs.sh"), name, "10m"],
                env=dict(os.environ, KUBECONFIG=str(ROOT / "kubeconfig" / "kube-ax.yaml")),
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            if "EGRESS done" in out:
                return dict(re.findall(r"EGRESS (\S+) (OK|BLOCKED)", out))
        raise TimeoutError(f"{name}: no result within 200 s")
    finally:
        run(["ax", "delete", "task", name, "-a", "default"], "ax-deployer.yaml")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "open"
    failed = 0
    for prefix, ok in EXPECT[mode].items():
        name = f"{prefix}egt-{uuid.uuid4().hex[:6]}"
        got = probe(name)
        for p in PROBES:
            want = "OK" if p in ok else "BLOCKED"
            mark = "ok  " if got.get(p) == want else "FAIL"
            failed += mark == "FAIL"
            print(f"{mark} {prefix:32s} {p:11s} want {want:7s} got {got.get(p)}", flush=True)
    print(f"{'PASS' if not failed else 'FAIL'}: {failed} mismatches ({mode})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
