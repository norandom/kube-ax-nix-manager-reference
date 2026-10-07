#!/usr/bin/env bash
# Install/upgrade the OpenObserve collector stack, equivalent to openobserve/o2-datasource's
# k8s/install.sh (same components and pinned versions) plus our sizing overlay.
#
#   just openobserve        # idempotent; re-run to apply changes
#
# Needs secrets/openobserve-auth (base64 "user:token", gitignored). Endpoint/org below.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
KUBECONFIG="$(realpath "${KUBECONFIG:-$root/kubeconfig/kube-ax.yaml}")"
export KUBECONFIG

O2_URL="${O2_URL:-http://10.0.0.54:40002}"
O2_ORG="${O2_ORG:-default}"
NAMESPACE="openobserve-collector"
CERT_MANAGER_VERSION="v1.20.0"
OTEL_OPERATOR_VERSION="0.138.0"
CHART_VERSION="0.5.0"

auth_file="$root/secrets/openobserve-auth"
[ -s "$auth_file" ] || { echo "missing $auth_file (base64 user:token)" >&2; exit 1; }

echo "== cert-manager $CERT_MANAGER_VERSION (OTel operator webhook certificates)"
kubectl apply -f "https://github.com/cert-manager/cert-manager/releases/download/${CERT_MANAGER_VERSION}/cert-manager.yaml" >/dev/null
kubectl -n cert-manager wait --for=condition=Available deploy --all --timeout=300s

# The upstream installer also adds four Prometheus-operator CRDs. Nothing here uses them (the
# collector's target allocator is disabled), and every CRD costs API-server memory: not installed.

echo "== OpenTelemetry operator $OTEL_OPERATOR_VERSION (collectors + auto-instrumentation)"
kubectl apply -f "https://github.com/open-telemetry/opentelemetry-operator/releases/download/v${OTEL_OPERATOR_VERSION}/opentelemetry-operator.yaml" >/dev/null
kubectl -n opentelemetry-operator-system wait --for=condition=Available deploy --all --timeout=300s
for _ in $(seq 60); do
  kubectl -n opentelemetry-operator-system get secret opentelemetry-operator-controller-manager-service-cert >/dev/null 2>&1 && break
  sleep 5
done

# The operator's Pod webhook re-encodes Pods with Kubernetes types that predate
# projected podCertificate volumes and silently drops them. Agent Substrate depends on those, so
# its namespaces are excluded; other namespaces (incl. ax-system) keep auto-instrumentation.
echo "== exclude Substrate namespaces from the OTel Pod webhook"
kubectl patch mutatingwebhookconfiguration opentelemetry-operator-mutating-webhook-configuration \
  --type json -p '[{"op":"add","path":"/webhooks/'"$(kubectl get mutatingwebhookconfiguration \
    opentelemetry-operator-mutating-webhook-configuration -o json \
    | python3 -c 'import sys,json; print([w["name"] for w in json.load(sys.stdin)["webhooks"]].index("mpod.kb.io"))')"'/namespaceSelector","value":
    {"matchExpressions":[{"key":"kubernetes.io/metadata.name","operator":"NotIn",
      "values":["ate-system","ax-workers","podcertificate-controller-system"]}]}}]'

echo "== collector chart $CHART_VERSION -> $O2_URL (org $O2_ORG)"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
helm repo add openobserve https://charts.openobserve.ai >/dev/null 2>&1 || true
helm repo update openobserve >/dev/null

secret_values="$(mktemp)"
trap 'rm -f "$secret_values"' EXIT
chmod 600 "$secret_values"
key="$(cat "$auth_file")"
# One exporter per stream: OpenObserve routes logs/traces by the stream-name header.
# otlphttp/openobserve carries metrics only (stream = metric name, prefixed k3s_ in values.yaml).
cat >"$secret_values" <<YAML
exporters:
  otlphttp/openobserve:
    endpoint: ${O2_URL}/api/${O2_ORG}
    headers:
      Authorization: "Basic ${key}"
  otlphttp/openobserve_logs:
    endpoint: ${O2_URL}/api/${O2_ORG}
    headers:
      Authorization: "Basic ${key}"
      stream-name: k3s_logs
  otlphttp/openobserve_traces:
    endpoint: ${O2_URL}/api/${O2_ORG}
    headers:
      Authorization: "Basic ${key}"
      stream-name: k3s_traces
  otlphttp/openobserve_k8s_events:
    endpoint: ${O2_URL}/api/${O2_ORG}
    headers:
      Authorization: "Basic ${key}"
      stream-name: k3s_events
YAML
helm upgrade --install o2c openobserve/openobserve-collector \
  --namespace "$NAMESPACE" --version "$CHART_VERSION" \
  -f "$root/platform/openobserve/values.yaml" -f "$secret_values" \
  --wait --timeout 300s

echo
kubectl -n "$NAMESPACE" get pods -o wide
kubectl -n "$NAMESPACE" get instrumentation 2>/dev/null || true
