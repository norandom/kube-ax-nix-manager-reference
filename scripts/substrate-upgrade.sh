#!/usr/bin/env bash
# Stop-the-world Substrate upgrade for this 2-node cluster (adapted from upstream docs/upgrade.md).
# Upstream's rolling upgrade clones every worker pool and drains node by node; with one pinned pool
# per node and short-lived AX tasks, a brief full stop is simpler and safer here.
#
#   scripts/substrate-upgrade.sh OLD_VERSION        e.g. v0.3.0
#
# Before: `just platform checkout patch images` for the new pin (scripts/platform.sh SUB_TAG).
# Needs: no AX task running (checked). AX tasks submitted during the stop fail with
# "no free workers"; clients retry. Rollback: same script from the old checkout.
set -euo pipefail
old="$1"
root="$(cd "$(dirname "$0")/.." && pwd)"
KUBECONFIG="$root/kubeconfig/kube-ax.yaml"; export KUBECONFIG
export PATH="$root/.cache/bin:$PATH" GOTOOLCHAIN=go1.27.1 GOTMPDIR="$root/.cache/tmp" TMPDIR="$root/.cache/tmp"
new="$(sed -n 's/^SUB_TAG=//p' "$root/scripts/platform.sh")"
sub="$root/.cache/platform/substrate"
[ "$(git -C "$sub" describe --tags)" = "$new" ] || { echo "checkout is not at $new" >&2; exit 1; }
[ "$old" != "$new" ] || { echo "old and new version are the same" >&2; exit 1; }
log() { printf '\n== %s\n' "$*"; }
setup() {
  (cd "$sub" && env -u PROJECT_ID -u CLUSTER_NAME -u CLUSTER_LOCATION -u ATE_INSTALL_KIND \
    NO_DEV_ENV=1 KUBECTL_CONTEXT=kube-ax go run ./cmd/ate-setup --image-repo ghcr.io/norandom \
    --image-tag "$new" --otlp-endpoint \
    http://o2c-openobserve-collector-gateway-collector.openobserve-collector.svc.cluster.local:4317 "$@")
}

log "preflight: $old -> $new, no actor on any worker"
kubectl get nodes -L ate.dev/substrate-version --no-headers
busy="$(kubectl ate get workers -o json | python3 -c 'import sys,json; print(sum(1 for w in json.load(sys.stdin).get("workers",[]) if w.get("status",{}).get("assignment",{}).get("actor")))')"
[ "$busy" = "0" ] || { echo "$busy worker(s) busy; wait until idle" >&2; exit 1; }

log "backup Postgres (schema migrations run on API server start)"
mkdir -p "$root/.cache/backups"
bk="$root/.cache/backups/atepg-$old-$(date +%Y%m%d-%H%M%S).sql.gz"
kubectl -n ate-system exec postgres-0 -c postgres -- pg_dump -U postgres atepg | gzip >"$bk"
echo "$bk ($(du -h "$bk" | cut -f1))"

log "1+2: ate-controller (also applies the new CRDs, as field manager ate-setup)"
setup deploy ate-controller

log "3: new atelet DaemonSet (0 pods until nodes are relabelled)"
setup deploy atelet
kubectl get ds -n ate-system -l app=atelet -L ate.dev/substrate-version

log "4: stop: delete worker pools, move both nodes to $new"
kubectl -n ax-workers get workerpools -o yaml >"$root/.cache/backups/workerpools-$old.yaml"
kubectl -n ax-workers delete workerpools --all --wait=true
kubectl label nodes -l "ate.dev/substrate-version=$old" "ate.dev/substrate-version=$new" --overwrite
kubectl -n ate-system rollout status ds -l "ate.dev/substrate-version=$new" --timeout=300s 2>/dev/null ||
  kubectl -n ate-system wait --for=condition=Ready pod -l app=atelet --timeout=300s

log "5: API server, then the rest (atenet, router, egress, postgres)"
setup deploy apiserver
setup deploy ate-system

log "6: worker pools on $new, old atelet DaemonSet removed"
"$root/scripts/platform.sh" pools
kubectl delete daemonset -n ate-system -l "app=atelet,ate.dev/substrate-version=$old" --ignore-not-found
(cd "$sub" && GOBIN="$root/.cache/bin" go install ./cmd/kubectl-ate)
# Clients of the headless api service keep the old API server pod's IP: restart them all.
kubectl -n ate-system rollout restart deploy/egress-watcher
kubectl -n ax-system rollout restart deploy/ax-controller deploy/ax-server deploy/ax-reaper
kubectl -n ax-workers wait --for=condition=Ready pod --all --timeout=600s
kubectl -n ax-workers get workerpools
echo "done: now run the AX smoke test and the egress tests"
