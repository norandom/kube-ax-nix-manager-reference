# kubeconfig/ (local only)

Cluster credentials live here and are **gitignored**; this repository is public.

| File | Written by | Access |
|---|---|---|
| `kube-ax.yaml` | `uv run just kubeconfig` (also at the end of `just k3s`) | cluster admin |
| `ax-deployer.yaml` | `uv run just ax-deployer` | submit and watch AX tasks only |

Both are mode 0600. `uv run just kube-local` merges them into `~/.kube/config`.
