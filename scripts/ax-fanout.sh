#!/usr/bin/env bash
# Fan one workflow out into N AX tasks (AX v0.3.1 maps 1 Task -> 1 actor and has no fan-out).
# Substrate places each actor on a random eligible gVisor worker; with one WorkerPool per node
# the tasks spread across kube-01 and kube-02.
#
#   just ax-fanout NAME COUNT IMAGE -- cmd args...     (cmd sees TASK_INDEX and TASK_COUNT)
#   e.g. just ax-fanout hash 6 ghcr.io/norandom/ax-task-runner@sha256:... -- sh -c 'echo $TASK_INDEX'
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$root/.cache/bin:$PATH"
name="$1"; count="$2"; image="$3"; shift 3
[ "${1:-}" = "--" ] && shift
[ $# -gt 0 ] || { echo "usage: ax-fanout NAME COUNT IMAGE -- cmd args..." >&2; exit 2; }
cmd_json="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "$@")"
for i in $(seq 0 $((count - 1))); do
  cat <<YAML
---
apiVersion: ax.io/v1alpha1
kind: Task
metadata:
  name: ${name}-${i}
  atespace: default
spec:
  image: "${image}"
  env:
    - {name: TASK_INDEX, value: "${i}"}
    - {name: TASK_COUNT, value: "${count}"}
  command: ${cmd_json}
YAML
done | ax apply -f -
echo "submitted ${count} tasks: ${name}-0..$((count - 1)); watch with: ax get tasks"
