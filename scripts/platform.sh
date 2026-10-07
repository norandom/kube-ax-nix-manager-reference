#!/usr/bin/env bash
# Install Agent Substrate (upstream main, pinned) + AX v0.3.1 on the kube-ax cluster (Requirements 16, 18).
#
#   just platform [STEP...]     # default: all steps in order; re-runnable
#
# Steps: checkout patch images s3 substrate pools egress ax reaper cli
# Snapshots go to the operator's existing rustfs (S3 API http://10.0.0.54:9000, bucket
# k3s-ate-snapshots); credentials from secrets/rustfs-credentials.
# Images are built from the pinned sources with ko (+ podman for envoy-dataplane) and pushed to
# ghcr.io/norandom; ate-setup then installs from those pre-built images.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
# Absolute, because steps cd into the upstream checkouts.
KUBECONFIG="$(realpath "${KUBECONFIG:-$root/kubeconfig/kube-ax.yaml}")"
export KUBECONFIG
plat="$root/.cache/platform"
export PATH="$root/.cache/bin:$PATH"
export GOTOOLCHAIN=go1.27.1
# /tmp on the workstation is a 1 GiB tmpfs; Substrate's Go build needs more scratch space.
export GOTMPDIR="$root/.cache/tmp" TMPDIR="$root/.cache/tmp"
mkdir -p "$GOTMPDIR"

REPO=ghcr.io/norandom
# Upstream main after v0.3.0: has #2019 + #2015 (enforced per-host TLS egress), stops before #2105,
# which drops a field AX v0.3.1 sends. SUB_TAG = `git describe` of that commit = image tag = the
# ate.dev/substrate-version label. Rollback: v0.3.0 + platform/substrate/kube-ax.patch.
SUB_TAG=v0.3.0-34-g715f2daa
SUB_COMMIT=715f2daa63371a19f54a0e37997569e3c2cb3cb9
AX_TAG=v0.3.1
AX_COMMIT=e70162a34037c221fe6fadefd98308c05a4ad8f3
SOURCE_LABEL="org.opencontainers.image.source=https://github.com/norandom/kube-ax-nix-manager"
OTLP_GRPC=http://o2c-openobserve-collector-gateway-collector.openobserve-collector.svc.cluster.local:4317

log() { printf '\n== %s\n' "$*"; }

step_checkout() {
  log "checkout pinned sources"
  mkdir -p "$plat"
  if [ ! -d "$plat/substrate" ]; then
    git clone -q https://github.com/agent-substrate/substrate "$plat/substrate"
    git -C "$plat/substrate" -c advice.detachedHead=false checkout -q "$SUB_COMMIT"
  fi
  [ -d "$plat/ax" ] || git -c advice.detachedHead=false clone -q --depth 1 --branch "$AX_TAG" \
    https://github.com/google/ax "$plat/ax"
  for spec in "substrate $SUB_COMMIT" "ax $AX_COMMIT"; do
    # shellcheck disable=SC2086  # deliberate word splitting into name/commit
    set -- $spec
    have="$(git -C "$plat/$1" rev-parse HEAD)"
    [ "$have" = "$2" ] || { echo "$1 is at $have, expected $2" >&2; exit 1; }
    echo "$1 @ ${have:0:12}"
  done
  command -v crane >/dev/null || GOBIN="$root/.cache/bin" go install github.com/google/go-containerregistry/cmd/crane@v0.20.6
}

step_patch() {
  log "apply platform/substrate/kube-ax-edits.py (S3/rustfs, no GCP, small Postgres, caps)"
  python3 "$root/platform/substrate/kube-ax-edits.py" "$plat/substrate"
  cd "$plat/ax"
  # AX's snapshot bucket defaults to a Google dev bucket; atelet reads it through the S3 client.
  sed -i '/name: AX_SNAPSHOTS_BUCKET/{n;s#value: ".*"#value: "gs://k3s-ate-snapshots/ax/"#}' deploy/ax-controller.yaml
  grep -q 'gs://k3s-ate-snapshots/ax/' deploy/ax-controller.yaml && echo "ax: AX_SNAPSHOTS_BUCKET -> gs://k3s-ate-snapshots/ax/"
}

step_images() {
  log "build + push images to $REPO (ko, podman)"
  gh auth token | ko login ghcr.io -u norandom --password-stdin >/dev/null
  gh auth token | podman login ghcr.io -u norandom --password-stdin >/dev/null
  cd "$plat/substrate"
  make build-images KO="$root/.cache/bin/ko" KO_DOCKER_REPO="$REPO" VERSION="$SUB_TAG" \
    KO_TAGS="--tags=$SUB_TAG" KO_DEFAULTPLATFORMS=linux/amd64 SKIP_IMAGES=./cmd/ateom-microvm \
    KO_FLAGS="--image-label=$SOURCE_LABEL"
  # Always rebuilt; podman's layer cache keeps an unchanged rebuild fast.
  podman build --platform linux/amd64 --label "$SOURCE_LABEL" \
    -t "$REPO/envoy-dataplane:$SUB_TAG" cmd/dataplane/envoy
  podman push "$REPO/envoy-dataplane:$SUB_TAG"
  # AX task runner: deliberately unlabelled, so it is a standalone package you can make public.
  if ! crane digest "$REPO/ax-task-runner:$AX_TAG" >/dev/null 2>&1; then
    cd "$plat/ax"
    GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" \
      -o bin/linux_amd64/ax-task-runner ./cmd/ax-task-runner
    podman build --platform linux/amd64 -t "$REPO/ax-task-runner:$AX_TAG" -f Dockerfile.task-runner .
    podman push "$REPO/ax-task-runner:$AX_TAG"
  fi
}

step_s3() {
  log "S3 credentials for the operator rustfs (http://10.0.0.54:9000, bucket k3s-ate-snapshots)"
  creds="$root/secrets/rustfs-credentials"
  [ -s "$creds" ] || { echo "missing $creds (access-key=..., secret-key=...)" >&2; exit 1; }
  kubectl create namespace ate-system --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl -n ate-system create secret generic ate-s3-credentials --from-env-file="$creds" \
    --dry-run=client -o yaml | kubectl apply -f -
}

step_substrate() {
  log "Substrate $SUB_TAG via ate-setup (pre-built images, telemetry -> OpenObserve gateway)"
  cd "$plat/substrate"
  env -u PROJECT_ID -u CLUSTER_NAME -u CLUSTER_LOCATION -u ATE_INSTALL_KIND \
    NO_DEV_ENV=1 KUBECTL_CONTEXT=kube-ax \
    go run ./cmd/ate-setup --image-repo "$REPO" --image-tag "$SUB_TAG" \
      --otlp-endpoint "$OTLP_GRPC" deploy ate-system
}

step_pools() {
  log "per-node gVisor WorkerPools"
  # shellcheck disable=SC2016  # envsubst takes the variable list literally
  WORKER_IMAGE="$REPO/ateom-gvisor:$SUB_TAG@$(crane digest "$REPO/ateom-gvisor:$SUB_TAG")" \
  SUBSTRATE_VERSION="$(kubectl get nodes kube-01 -o jsonpath='{.metadata.labels.ate\.dev/substrate-version}')" \
    envsubst '${WORKER_IMAGE} ${SUBSTRATE_VERSION}' <"$root/platform/substrate/workerpools.yaml" | kubectl apply -f -
  kubectl -n ax-workers get workerpools
}

step_egress() {
  log "egress-watcher (policy for ousast-engine-* actors) + atenet egress cache TTL"
  cd "$root/tools/egress-watcher"
  go test ./...
  img="$(KO_DOCKER_REPO="$REPO" ko build --base-import-paths --platform=linux/amd64 \
    --image-label="$SOURCE_LABEL" --tags=latest .)"
  kubectl apply -f "$root/platform/substrate/egress-netpol.yaml"
  # scoped = per-prefix host lists (live since 2026-10-06); open = public HTTPS for all (fallback).
  routes="$root/platform/substrate/egress-routes/${EGRESS_ROUTES:-scoped}.json"
  kubectl -n ate-system create configmap egress-watcher-routes --from-file=routes.json="$routes" \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n ate-system delete configmap egress-watcher-policy --ignore-not-found
  # shellcheck disable=SC2016  # envsubst takes the variable list literally
  WATCHER_IMAGE="$img" envsubst '${WATCHER_IMAGE}' <"$root/platform/substrate/egress-watcher.yaml" | kubectl apply -f -
  kubectl -n ate-system rollout restart deploy/egress-watcher  # pick up route changes
  kubectl -n ate-system rollout status deploy/egress-watcher --timeout=180s
}

step_ax() {
  log "AX $AX_TAG (redis, controller, server)"
  cd "$plat/ax"
  kubectl apply -f deploy/redis.yaml
  KO_DOCKER_REPO="$REPO" ko apply --base-import-paths --platform=linux/amd64 \
    --image-label="$SOURCE_LABEL" -f deploy/ax-controller.yaml -f deploy/ax-server.yaml
  kubectl -n ax-system rollout status deploy/ax-controller --timeout=300s
  kubectl -n ax-system rollout status deploy/ax-server --timeout=300s
  # Go auto-instrumentation -> OpenObserve (per Deployment: each pod has a different binary).
  for d in ax-controller ax-server; do
    kubectl -n ax-system patch deploy "$d" --type merge -p "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{
      \"instrumentation.opentelemetry.io/inject-go\":\"openobserve-collector/openobserve-go\",
      \"instrumentation.opentelemetry.io/otel-go-auto-target-exe\":\"/ko-app/$d\"}}}}}" >/dev/null
  done
}

step_reaper() {
  log "ax-reaper (delete ousast-engine-* tasks 10 min after their command exited)"
  cd "$root/tools/ax-reaper"
  go test ./...
  img="$(KO_DOCKER_REPO="$REPO" ko build --base-import-paths --platform=linux/amd64 \
    --image-label="$SOURCE_LABEL" --tags=latest .)"
  # shellcheck disable=SC2016  # envsubst takes the variable list literally
  REAPER_IMAGE="$img" envsubst '${REAPER_IMAGE}' <"$root/platform/ax/reaper.yaml" | kubectl apply -f -
  kubectl -n ax-system rollout status deploy/ax-reaper --timeout=180s
}

step_cli() {
  log "ax CLI $AX_TAG -> .cache/bin/ax"
  GOBIN="$root/.cache/bin" go install "github.com/google/ax/cmd/ax@$AX_TAG"
  # `ax --help` exits 1 by design; just confirm the binary is there.
  [ -x "$root/.cache/bin/ax" ] && echo "ax installed: $root/.cache/bin/ax"
}

steps=("$@")
[ ${#steps[@]} -gt 0 ] || steps=(checkout patch images s3 substrate pools egress ax reaper cli)
for s in "${steps[@]}"; do "step_$s"; done
