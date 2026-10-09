#!/usr/bin/env python3
"""Disk and eviction alerts in OpenObserve, delivered to Discord (docs/guardrails.md).

    scripts/openobserve-alerts.py discover   # disk metric names, labels, latest values
    scripts/openobserve-alerts.py apply      # create/update template, destination, alerts
    scripts/openobserve-alerts.py test       # post a sample alert message to Discord
    scripts/openobserve-alerts.py e2e        # temporary alert that must fire via OpenObserve

(run with `uv run python`)

Secrets (gitignored): secrets/openobserve-admin ("user:password", alert rights),
secrets/discord-webhook (URL), secrets/discord-user-id (who gets mentioned).
"""

from __future__ import annotations

import base64
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
O2 = "http://10.0.0.54:40002"
ORG = "default"
NAME = "kube-ax-discord"  # template and destination
FS_STREAM = "k3s_system_filesystem_usage"
EVENTS_STREAM = "k3s_events"  # Kubernetes events; reason in body_object_reason
# Volumes we care about, as the hostmetrics receiver reports them (see `discover`).
MOUNTS = ("/", "/var/lib/rancher/k3s/agent/containerd", "/var/lib/rancher/k3s/storage")


def secret(name: str) -> str:
    return (ROOT / "secrets" / name).read_text().strip()


def api(method: str, path: str, body: object | None = None) -> object:
    auth = base64.b64encode(secret("openobserve-admin").encode()).decode()
    req = urllib.request.Request(
        O2 + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path}: HTTP {e.code} {e.read().decode()[:400]}") from None
    return json.loads(raw) if raw else None


def search(sql: str, minutes: int = 15) -> list[dict]:
    end = int(time.time() * 1_000_000)
    body = {"query": {"sql": sql, "start_time": end - minutes * 60_000_000, "end_time": end}}
    return api("POST", f"/api/{ORG}/_search?type=metrics", body).get("hits", [])


def discover() -> None:
    streams = api("GET", f"/api/{ORG}/streams?type=metrics")["list"]
    names = sorted(s["name"] for s in streams)
    print(f"{len(names)} metric streams; filesystem-related:")
    for n in names:
        if "filesystem" in n or "fs_" in n:
            print("  ", n)
    hits = search(f'SELECT * FROM "{FS_STREAM}" ORDER BY _timestamp DESC LIMIT 400')
    if not hits:
        print(f"no rows in {FS_STREAM} in the last 15 min")
        return
    print("columns:", sorted(hits[0]))
    seen = {}
    for h in hits:
        key = tuple(str(h.get(k)) for k in ("k8s_node_name", "mountpoint", "state"))
        seen.setdefault(key, h.get("value"))
    for k, v in sorted(seen.items()):
        print("  ", k, v)


def free_pct_sql() -> str:
    mounts = ", ".join(f"'{m}'" for m in MOUNTS)
    # Average per node/volume/state over the alert period, then free / (used + free + reserved).
    # k8s_node_name is the node; host_name is the collector pod.
    return (
        "SELECT k8s_node_name, mountpoint, "
        "ROUND(100.0 * SUM(CASE WHEN state = 'free' THEN v END) / SUM(v), 1) AS free_pct "
        "FROM (SELECT k8s_node_name, mountpoint, state, AVG(value) AS v "
        f'FROM "{FS_STREAM}" WHERE mountpoint IN ({mounts}) '
        "GROUP BY k8s_node_name, mountpoint, state) "
        "GROUP BY k8s_node_name, mountpoint"
    )


def alerts() -> list[dict]:
    base = {
        "stream_type": "metrics",
        "stream_name": FS_STREAM,
        "is_real_time": False,
        "enabled": True,
        "destinations": [NAME],
        "tz_offset": 0,
    }
    out = []
    for name, pct, prio in (
        ("kube-ax-disk-free-below-25pct", 25, 3),
        ("kube-ax-disk-free-below-15pct", 15, 1),
    ):
        out.append(
            base
            | {
                "name": name,
                "priority": prio,
                "description": f"A node volume (/, containerd or storage) has under {pct}% free. "
                "Kubelet evicts at 10% (/) and 15% (containerd). See docs/guardrails.md.",
                "query_condition": {
                    "type": "sql",
                    "sql": f"SELECT k8s_node_name, mountpoint, free_pct FROM ({free_pct_sql()}) "
                    f"WHERE free_pct < {pct}",
                },
                "trigger_condition": {
                    "period": 10,
                    "frequency": 5,  # minutes (the API doc says seconds; it is minutes)
                    "frequency_type": "minutes",
                    "operator": ">=",
                    "threshold": 1,
                    "silence": 60 if pct == 25 else 30,
                },
            }
        )
    out.append(
        base
        | {
            "name": "kube-ax-node-pressure",
            "stream_type": "logs",
            "stream_name": EVENTS_STREAM,
            "priority": 2,
            "description": "A node reports disk or memory pressure, or 20+ pods were evicted "
            "in 10 min (eviction storm). Single evictions are normal and do not alert.",
            "query_condition": {
                "type": "sql",
                "sql": "SELECT body_object_reason AS reason, COUNT(*) AS events "
                f'FROM "{EVENTS_STREAM}" WHERE body_object_reason IN '
                "('EvictionThresholdMet', 'NodeHasDiskPressure', 'NodeHasMemoryPressure', "
                "'Evicted') GROUP BY body_object_reason "
                "HAVING body_object_reason <> 'Evicted' OR COUNT(*) >= 20",
            },
            "trigger_condition": {
                "period": 10,
                "frequency": 5,  # minutes (the API doc says seconds; it is minutes)
                "frequency_type": "minutes",
                "operator": ">=",
                "threshold": 1,
                "silence": 30,
            },
        }
    )
    return out


def template() -> dict:
    user = secret("discord-user-id")
    content = (
        f"<@{user}> **{{alert_name}}** ({{alert_count}} rows, last {{alert_period}} min)\\n"
        "{rows}\\n{alert_url}"
    )
    body = '{"content": "' + content + '", "allowed_mentions": {"users": ["' + user + '"]}}'
    return {"name": NAME, "type": "http", "title": "", "body": body}


def upsert(kind: str, item: dict, existing: list[dict]) -> None:
    path = f"/api/{ORG}/alerts/{kind}"
    if any(e.get("name") == item["name"] for e in existing):
        api("PUT", f"{path}/{item['name']}", item)
        print(f"updated {kind[:-1]} {item['name']}")
    else:
        api("POST", path, item)
        print(f"created {kind[:-1]} {item['name']}")


def apply() -> None:
    upsert("templates", template(), api("GET", f"/api/{ORG}/alerts/templates"))
    dest = {
        "name": NAME,
        "type": "http",
        "url": secret("discord-webhook"),
        "method": "post",
        "template": NAME,
        "skip_tls_verify": False,
        "headers": {"Content-Type": "application/json"},
    }
    upsert("destinations", dest, api("GET", f"/api/{ORG}/alerts/destinations"))
    current = api("GET", f"/api/v2/{ORG}/alerts").get("list", [])
    by_name = {a.get("name"): a.get("alert_id") or a.get("id") for a in current}
    for a in alerts():
        if a["name"] in by_name:
            api("PUT", f"/api/v2/{ORG}/alerts/{by_name[a['name']]}", a)
            print(f"updated alert {a['name']}")
        else:
            api("POST", f"/api/v2/{ORG}/alerts", a)
            print(f"created alert {a['name']}")
    rows = search(free_pct_sql())
    print("current free space:")
    for r in rows:
        print(f"  {r.get('k8s_node_name')} {r.get('mountpoint')}: {r.get('free_pct')}% free")


def test() -> None:
    user = secret("discord-user-id")
    msg = {
        "content": f"<@{user}> **kube-ax-disk-free-below-25pct** (test message, nothing is wrong)\n"
        "kube-01 /var/lib/rancher/k3s/agent/containerd: 24.0% free",
        "allowed_mentions": {"users": [user]},
    }
    req = urllib.request.Request(
        secret("discord-webhook"),
        data=json.dumps(msg).encode(),
        # Discord's Cloudflare front rejects Python's default User-Agent with 403.
        headers={"Content-Type": "application/json", "User-Agent": "kube-ax-alerts/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        print("discord:", r.status)


def e2e() -> None:
    """Create a temporary alert that must fire (free < 99 %), wait for OpenObserve to post it to
    Discord, then delete it."""
    alert = alerts()[0] | {"name": "kube-ax-e2e-test", "priority": 5}
    alert["query_condition"] = alert["query_condition"] | {
        "sql": f"SELECT k8s_node_name, mountpoint, free_pct FROM ({free_pct_sql()}) "
        "WHERE free_pct < 99"
    }
    alert["trigger_condition"] = alert["trigger_condition"] | {"frequency": 1, "silence": 0}
    created = api("POST", f"/api/v2/{ORG}/alerts", alert)
    alert_id = created.get("id") or created.get("alert_id")
    start = int(time.time() * 1_000_000)
    print("created kube-ax-e2e-test; waiting for it to fire (up to 5 min)")
    try:
        for _ in range(30):
            time.sleep(10)
            got = api("GET", f"/api/v2/{ORG}/alerts/{alert_id}")
            fired = got.get("last_triggered_at") or 0
            if fired >= start:
                print(
                    f"fired at {time.strftime('%H:%M:%S', time.localtime(fired / 1e6))}; "
                    "check Discord for the message"
                )
                break
        else:
            print("did not fire within 5 min")
    finally:
        api("DELETE", f"/api/v2/{ORG}/alerts/{alert_id}")
        print("deleted kube-ax-e2e-test")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "discover"
    {"discover": discover, "apply": apply, "test": test, "e2e": e2e}[action]()
