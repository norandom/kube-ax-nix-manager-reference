#!/usr/bin/env bash
# Deploy/refresh the Gitea Actions runners (platform/gitea-runner/runners.yaml).
#   just gitea-runner
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
KUBECONFIG="$(realpath "${KUBECONFIG:-$root/kubeconfig/kube-ax.yaml}")"
export KUBECONFIG
token="$root/secrets/gitea-runner-token"
[ -s "$token" ] || { echo "missing $token (runner registration token from Gitea)" >&2; exit 1; }
# Namespace and token Secret first, so the runners start with their registration token.
kubectl create namespace gitea-runner --dry-run=client -o yaml | kubectl apply -f -
kubectl -n gitea-runner create secret generic runner-secret --from-file=token="$token" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "$root/platform/gitea-runner/runners.yaml"
kubectl -n gitea-runner rollout status statefulset/runner-pods --timeout=300s
kubectl -n gitea-runner rollout status statefulset/runner-dind --timeout=600s
kubectl -n gitea-runner get pods -o wide
