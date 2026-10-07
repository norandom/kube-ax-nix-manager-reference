#!/usr/bin/env bash
# Point the local Kubernetes tools (kubectl, k9s, ktop, helm) at this cluster.
#
# Merges kubeconfig/<cluster>.yaml into ~/.kube/config (all those tools read it by default),
# replacing any previous entries for this cluster and keeping every other context. Existing
# config is backed up first. Re-run after `just kubeconfig` (e.g. yearly k3s cert rotation).
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cluster="${1:-kube-ax}"
src="$root/kubeconfig/$cluster.yaml"
dest="${KUBE_LOCAL_CONFIG:-$HOME/.kube/config}"

[ -f "$src" ] || { echo "missing $src: run 'uv run just kubeconfig' first" >&2; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl not found" >&2; exit 1; }

mkdir -p "$(dirname "$dest")"
chmod 700 "$(dirname "$dest")"

tmp="$(mktemp)"
trap 'rm -f "$tmp" "$tmp.old"' EXIT

if [ -s "$dest" ]; then
  cp -p "$dest" "$dest.bak.$(date +%Y%m%d-%H%M%S)"
  cp "$dest" "$tmp.old"
  # Drop stale entries for this cluster so the merge below takes the repo's current creds.
  for kind in context cluster user; do
    KUBECONFIG="$tmp.old" kubectl config "delete-$kind" "$cluster" >/dev/null 2>&1 || true
  done
  KUBECONFIG="$tmp.old:$src" kubectl config view --flatten >"$tmp"
else
  KUBECONFIG="$src" kubectl config view --flatten >"$tmp"
fi

install -m 600 "$tmp" "$dest"
KUBECONFIG="$dest" kubectl config use-context "$cluster" >/dev/null

echo "$dest: context '$cluster' active ($(KUBECONFIG="$dest" kubectl config get-contexts -o name | wc -l) context(s) total)"
echo
KUBECONFIG="$dest" kubectl get nodes -o wide
echo
for tool in k9s ktop helm; do
  if command -v "$tool" >/dev/null; then
    printf '%-5s %s\n' "$tool" "$(command -v "$tool")"
  else
    printf '%-5s not installed\n' "$tool"
  fi
done
if command -v helm >/dev/null; then
  KUBECONFIG="$dest" helm list -A >/dev/null && echo "helm  reaches the cluster"
fi
cat <<'TXT'

Try:  kubectl get pods -A     k9s     ktop     helm list -A
(metrics-server is installed, so `kubectl top` and ktop's usage columns work.)
TXT
