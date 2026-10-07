# Upgrading Agent Substrate

**In one sentence:** pin a new upstream commit, let our anchored edits re-apply our changes, build the
images, then switch both nodes in one short stop (about 3 minutes, no tasks running), and prove egress
and health before telling users.

Last done: 2026-10-06, v0.3.0 → main `715f2daa` (`v0.3.0-34-g715f2daa`), 08:03–08:06.

## Why a "stop", not upstream's rolling upgrade

Upstream's runbook (`docs/upgrade.md` in the Substrate repo) clones every worker pool and drains node by
node, so long-lived actors keep their state. Here, every worker pool is pinned to one node, AX tasks are
short, and AX keeps no state worth migrating. A short full stop is simpler and has fewer ways to go
wrong. Tasks submitted during the stop fail with "no free workers" and clients retry.

## Before you pick a commit

Check what AX sends. AX v0.3.1 is built against an older Substrate API:

- upstream **#2105** (2026-10-02) removed `snapshotConfig.onResume`. AX v0.3.1 still sets it, so
  Substrate after #2105 rejects every AX task. We stay on `715f2daa`, the last commit before it.
- Moving past it needs an AX release that stops sending that field. Check AX's
  `internal/substrate/client.go` (`OnResume`) before bumping.

Read upstream's "Behavior changes" and "Breaking change" notes for every commit in the range
(`git log --oneline OLD..NEW`).

## Steps

| # | Do | Command |
|---|---|---|
| 1 | Pin the commit: `SUB_TAG` (`git describe` of the commit) and `SUB_COMMIT` in `scripts/platform.sh`, the same in `platform/versions.toml` | edit |
| 2 | Check out and apply our changes; each edit must print `applied` or `present`, never `MISSING` | `uv run just platform checkout patch` |
| 3 | Build and push images (about 10 min, no cluster impact) | `uv run just platform images` |
| 4 | Baseline egress test on the **old** version | `uv run python scripts/egress-test.py scoped` |
| 5 | Wait until no task runs (the script refuses otherwise), then switch | `scripts/substrate-upgrade.sh OLD_VERSION` |
| 6 | Egress test again, then the cluster check | `scripts/egress-test.py scoped`, `uv run just verify` |
| 7 | Commit, then tell users the time of the stop and any failed task names | — |

What step 5 does:

1. refuses if any worker hosts an actor;
2. backs up Postgres to `.cache/backups/` (schema migrations run when the new API server starts);
3. deploys the new controller (it also applies the new CRDs) and the new node agent (atelet), which has 0 pods until nodes move;
4. deletes the worker pools and moves both nodes' `ate.dev/substrate-version` label to the new version;
5. deploys the API server, then the rest (router, egress gateway, Postgres settings);
6. recreates the per-node worker pools on the new image, removes the old atelet and installs the new `kubectl ate`;
7. restarts every client of the Substrate API (see below).

## The one surprise: clients keep the old API server address

Substrate's API service is "headless": clients get the pod's own IP, not a stable service IP. AX, the
egress watcher and the reaper look it up once and keep it. After the API server pod is replaced, they
keep dialling the old address, and AX tasks fail with
`dial tcp 10.42.x.y:443: i/o timeout` in the ax-controller log.

The upgrade script restarts them. The same thing can happen whenever the API server pod is recreated
(node restart, eviction). The fix is the same:

```bash
kubectl -n ax-system rollout restart deploy/ax-controller deploy/ax-server deploy/ax-reaper
kubectl -n ate-system rollout restart deploy/egress-watcher
```

## Our changes to Substrate

`platform/substrate/kube-ax-edits.py` makes 13 small edits. Each one looks for exact upstream text, so
an upstream change shows up as `MISSING` instead of a silently wrong install:

| Edit | Why |
|---|---|
| snapshots to rustfs S3 (10.0.0.54:9000) instead of GCS | no Google Cloud here |
| no image credential provider plugins | GKE/EKS host paths; our task images are public |
| atelet `GOMEMLIMIT=160MiB`, `GOGC=50` | node agent was 184–261 MiB |
| image cache cap 10 GiB, cleanup 75 → 65 % | the 2026-10-05 disk outage ([guardrails](guardrails.md)) |
| API server 1 replica, Postgres 250m / 384 Mi / 8 Gi pinned to kube-01 | small cluster |
| no PodMonitoring (GKE-only object) | the CRD doesn't exist here |
| egress "no policy yet" cache 2 s (upstream 10 s) | the watcher attaches policies ~1 s after start |
| one actor per worker (upstream 1000) | one task per worker, predictable memory |

## Rolling back

The old images stay in `ghcr.io/norandom` under the old tag. Check out the old commit with
`git checkout` (for v0.3.0: `kube-ax.patch` instead of the edits script, and the v0.3.0 values in
`scripts/platform.sh`), then run `scripts/substrate-upgrade.sh NEW_VERSION` from it. The Postgres backup
from step 5 restores the database if a migration has to be undone.

**You know it worked when** `kubectl get nodes -L ate.dev/substrate-version` shows the new version on
both nodes, the egress test passes, and `uv run just verify` passes.
