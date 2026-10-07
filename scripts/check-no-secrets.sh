#!/usr/bin/env bash
# pre-commit hook: refuse to commit secrets/ and kubeconfig/ files except their READMEs (Req 9.7).
set -euo pipefail
bad=0
for f in "$@"; do
  case "$f" in
    secrets/README.md|kubeconfig/README.md) ;;
    kubeconfig/*) echo "refusing to commit kubeconfig (credentials): $f" >&2; bad=1 ;;
    secrets/*) echo "refusing to commit secret file: $f" >&2; bad=1 ;;
  esac
done
exit "$bad"
