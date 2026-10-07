#!/usr/bin/env bash
# Publish the current tree to the public repo as ONE new commit (no private history).
#   just publish ["message"]
# The private repo (origin) keeps the full history, which contains old credentials: only the
# public-main branch, built here from a tree snapshot, ever goes to the public remote.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
public_url="https://github.com/norandom/kube-ax-nix-manager-reference.git"
name="norandom"
email="11855163+norandom@users.noreply.github.com"   # GitHub noreply: keeps the real address private
msg="${1:-Update from the private repository ($(git rev-parse --short HEAD))}"

[ -z "$(git status --porcelain)" ] || { echo "commit or stash your changes first" >&2; exit 1; }
git remote get-url public >/dev/null 2>&1 || git remote add public "$public_url"

tree="$(git rev-parse 'HEAD^{tree}')"
parent=()
if git rev-parse -q --verify public-main >/dev/null; then
  [ "$(git rev-parse 'public-main^{tree}')" != "$tree" ] || { echo "public-main already has this tree"; exit 0; }
  parent=(-p public-main)
fi

# Scan exactly what would be published: a single-branch clone of the new commit.
export GIT_AUTHOR_NAME="$name" GIT_AUTHOR_EMAIL="$email" GIT_COMMITTER_NAME="$name" GIT_COMMITTER_EMAIL="$email"
commit="$(git commit-tree "$tree" "${parent[@]}" -m "$msg")"
scan="$(mktemp -d "$root/.cache/publish-scan.XXXX")"
trap 'rm -rf "$scan"' EXIT
git branch -f publish-candidate "$commit"
git clone -q --single-branch --branch publish-candidate "$root" "$scan/repo"
git branch -D publish-candidate >/dev/null
gitleaks="$(command -v gitleaks || find ~/.cache/pre-commit -name gitleaks -type f -perm -u+x | head -1)"
[ -n "$gitleaks" ] || { echo "gitleaks not found (run pre-commit once)" >&2; exit 1; }
"$gitleaks" git --no-banner -c "$root/.gitleaks.toml" "$scan/repo"
for p in kubeconfig secrets; do
  extra="$(git -C "$scan/repo" ls-files "$p" | grep -v "^$p/README.md$" || true)"
  [ -z "$extra" ] || { echo "refusing: $extra" >&2; exit 1; }
done

git branch -f public-main "$commit"
git push public public-main:main
echo "published $(git rev-parse --short public-main) to $public_url"
