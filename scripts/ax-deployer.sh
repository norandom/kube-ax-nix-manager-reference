#!/usr/bin/env bash
# Create/refresh the ax-deployer identity and write kubeconfig/ax-deployer.yaml (local only, gitignored).
#   just ax-deployer
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
export KUBECONFIG="$root/kubeconfig/kube-ax.yaml"
kubectl apply -f "$root/platform/ax/deployer-rbac.yaml" >/dev/null
for _ in $(seq 30); do
  token="$(kubectl -n ax-system get secret ax-deployer-token -o jsonpath='{.data.token}' 2>/dev/null | base64 -d)"
  [ -n "$token" ] && break; sleep 1
done
[ -n "$token" ] || { echo "token not issued" >&2; exit 1; }
server="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')"
ca="$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')"
out="$root/kubeconfig/ax-deployer.yaml"
umask 077
cat >"$out" <<YAML
apiVersion: v1
kind: Config
clusters:
- name: ax-deployer
  cluster:
    server: ${server}
    certificate-authority-data: ${ca}
users:
- name: ax-deployer
  user:
    token: ${token}
contexts:
- name: ax-deployer
  context:
    cluster: ax-deployer
    user: ax-deployer
    namespace: ax-system
current-context: ax-deployer
YAML
chmod 600 "$out"
echo "wrote $out (context ax-deployer)"
"$root/scripts/ax-guide-sync.sh"
