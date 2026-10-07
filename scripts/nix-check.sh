#!/usr/bin/env bash
# Evaluate every nixosConfiguration (fixtures, and node if node.json exists) without local Nix,
# inside a pinned nixos/nix container (Req 1.2a). Caches /nix and ~/.cache in podman volumes.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
image="${NIX_IMAGE:-docker.io/nixos/nix:2.35.2}"
podman run --rm \
  -v nixstore-kubeax:/nix -v nixcache-kubeax:/root/.cache \
  -v "$root/nix":/src:Z "$image" sh -euc '
export NIX_CONFIG="experimental-features = nix-command flakes"
cd /src
[ -f flake.lock ] || nix flake lock
names=$(nix eval --raw path:/src#nixosConfigurations \
  --apply "c: builtins.concatStringsSep \" \" (builtins.attrNames c)")
fail=0
for c in $names; do
  if out=$(nix eval --raw "path:/src#nixosConfigurations.$c.config.system.build.toplevel.drvPath" 2>&1); then
    echo "ok   $c  $out"
  else
    echo "FAIL $c"; echo "$out" | tail -15; fail=1
  fi
done
exit $fail'
