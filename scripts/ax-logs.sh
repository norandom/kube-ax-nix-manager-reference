#!/usr/bin/env bash
# Print the latest run of one AX task: its stdout/stderr plus runner lines (incl. exit code), from
# the worker pod logs. Substrate wraps actor output as JSON with labels.ate.actor.name/uid; AX names
# the actor after the task. A re-created task keeps its name but gets a new actor UID, so only the
# most recent UID is shown. Works with the restricted ax-deployer kubeconfig.
#   just ax-logs TASK [SINCE=2h]
set -euo pipefail
task="$1"; since="${2:-2h}"
for pod in $(kubectl -n ax-workers get pods -o name); do
  kubectl -n ax-workers logs "$pod" --since="$since" --timestamps 2>/dev/null
done | sort | python3 -c '
import sys, json
task = sys.argv[1]
runs = {}          # actor uid -> [lines], insertion order = first appearance in time
for line in sys.stdin:
    ts, _, rest = line.partition(" ")
    if not rest.startswith("{\"labels\""):
        continue
    try:
        d = json.loads(rest)
    except ValueError:
        continue
    labels = d.get("labels", {})
    if labels.get("ate.actor.name") != task:
        continue
    msg = d.get("message", "").rstrip()
    if msg.startswith(("I1", "W1", "E1")):       # gVisor (runsc) sandbox chatter
        continue
    runs.setdefault(labels.get("ate.actor.uid", "?"), []).append(f"{ts[11:19]}  {msg}")
if not runs:
    sys.exit(f"no output for task {task!r} in the selected time window")
uid = list(runs)[-1]
print(f"# task {task}, actor uid {uid} ({len(runs)} run(s) in window, showing latest)")
print("\n".join(runs[uid]))
' "$task"
