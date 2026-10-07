#!/usr/bin/env bash
# One-off: move Substrate's /var/lib/ate from the OS volume onto the containerd volume on ONE node,
# without losing the image cache. New nodes get the bind mount from nix/modules/k3s.nix directly.
#   scripts/migrate-ate-dir.sh <node-ip> <node-name>
# Requires: no AX task running on that node. The other node keeps serving.
set -euo pipefail
ip="$1"; node="$2"
root="$(cd "$(dirname "$0")/.." && pwd)"
KUBECONFIG="$root/kubeconfig/kube-ax.yaml"; export KUBECONFIG
export PATH="$root/.cache/bin:$PATH"
label=ate.dev/substrate-version
version="$(kubectl get node "$node" -o jsonpath="{.metadata.labels.ate\.dev/substrate-version}")"
[ -n "$version" ] || { echo "$node has no $label label" >&2; exit 1; }

busy="$(kubectl ate get actors -a default 2>/dev/null | awk -v n="$node" 'NR>1 && $5 ~ /^ax-workers\// {print $5}' | while read -r w; do
  kubectl -n ax-workers get pod "${w#ax-workers/}" -o jsonpath='{.spec.nodeName}{"\n"}' 2>/dev/null; done | grep -c "^$node$" || true)"
[ "$busy" = "0" ] || { echo "$node runs $busy actor(s); wait until it is idle" >&2; exit 1; }

echo "== $node: stop atelet + worker (remove $label=$version)"
kubectl label node "$node" "$label-"
kubectl -n ate-system wait --for=delete pod -l app=atelet --field-selector "spec.nodeName=$node" --timeout=180s || true
kubectl -n ax-workers wait --for=delete pod --field-selector "spec.nodeName=$node" --timeout=180s || true

echo "== $node: copy /var/lib/ate to the containerd volume"
ssh -o BatchMode=yes "ops@$ip" 'set -e
  sudo rm -rf /var/lib/rancher/k3s/agent/containerd/ate   # no rsync on the nodes: plain cp -a
  sudo cp -a /var/lib/ate /var/lib/rancher/k3s/agent/containerd/ate
  sudo mv /var/lib/ate /var/lib/ate.pre-migration && sudo mkdir /var/lib/ate'

echo "== $node: switch NixOS (bind mount): ate_on_containerd -> True in inventory.py"
python3 - "$root/inventory.py" "$node" <<'PY'
import pathlib, sys
p, node = pathlib.Path(sys.argv[1]), sys.argv[2]
lines = p.read_text().splitlines(keepends=True)
hit = [i for i, l in enumerate(lines) if f'"node_name": "{node}"' in l and '"ate_on_containerd": False' in l]
if len(hit) != 1:
    sys.exit(f"inventory.py: expected one {node} line with ate_on_containerd False")
lines[hit[0]] = lines[hit[0]].replace('"ate_on_containerd": False', '"ate_on_containerd": True')
p.write_text("".join(lines))
PY
uv run --project "$root" pyinfra "$root/inventory.py" "$root/deploys/k3s.py" --limit "$ip" -y >/dev/null
ssh -o BatchMode=yes "ops@$ip" 'set -e
  findmnt -no SOURCE,TARGET /var/lib/ate
  test -d /var/lib/ate/image-cache
  sudo rm -rf /var/lib/ate.pre-migration
  df -h --output=target,used,avail / /var/lib/rancher/k3s/agent/containerd'

echo "== $node: start atelet + worker again"
kubectl label node "$node" "$label=$version"
kubectl -n ate-system rollout status daemonset -l app=atelet --timeout=300s 2>/dev/null || sleep 30
kubectl -n ax-workers get pods -o wide --field-selector "spec.nodeName=$node"
