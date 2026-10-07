#!/usr/bin/env bash
# pre-commit: gofmt + go vet for tools/egress-watcher.
set -euo pipefail
cd "$(dirname "$0")/../tools/egress-watcher"
unformatted="$(gofmt -l .)"
if [ -n "$unformatted" ]; then
  echo "gofmt needed in: $unformatted" >&2
  exit 1
fi
GOTOOLCHAIN=go1.27.1 go vet ./...
