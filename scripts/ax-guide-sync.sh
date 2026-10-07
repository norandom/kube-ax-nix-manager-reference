#!/usr/bin/env bash
# Render docs/ax-guide.md and docs/ax-handoff.md with kubeconfig/ax-deployer.yaml embedded, into the
# gitignored .cache/handoff/. The committed docs keep a placeholder (the repo is public).
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$root/.cache/handoff"
for doc in ax-guide ax-handoff; do
python3 - "$root/docs/$doc.md" "$root/kubeconfig/ax-deployer.yaml" "$root/.cache/handoff/$doc.md" <<'PY'
import re, sys, pathlib
guide, cfg, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]).read_text().rstrip("\n"), pathlib.Path(sys.argv[3])
s = guide.read_text()
begin = re.search(r"<!-- BEGIN ax-deployer kubeconfig[^>]*-->\n", s)
end = s.index("<!-- END ax-deployer kubeconfig -->")
s = s[: begin.end()] + "\n```yaml\n# kubeconfig/ax-deployer.yaml\n" + cfg + "\n```\n\n" + s[end:]
out.write_text(s)
out.chmod(0o600)
PY
echo ".cache/handoff/$doc.md: rendered with the ax-deployer kubeconfig (local only)"
done
