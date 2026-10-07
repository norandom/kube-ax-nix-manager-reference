# Gitea Actions runners

Two runners (gitea/runner **v4.1.0**) are registered with **<https://code.because-security.com>** and run on
this cluster. CI is **second in line**: AX keeps its fixed share, CI uses what's left, and **jobs wait
instead of failing** when the cluster is busy.

## Which runner a workflow gets

| `runs-on:` | Runner | How the job runs | Use for |
|---|---|---|---|
| `ubuntu-latest` or `kube-ax` | `kube-ax-pods` | its own Kubernetes pod (`docker.gitea.com/runner-images:ubuntu-latest`), no Docker daemon, not privileged | tests, linting, builds that don't need Docker: the default |
| `dind` | `kube-ax-dind` | inside a Docker daemon next to the runner (**privileged**) | Docker actions, `docker://` steps, building/pushing images |

```yaml
jobs:
  test:
    runs-on: ubuntu-latest   # pod runner
  image:
    runs-on: dind            # Docker-in-Docker runner (trusted repos only)
```

The pod runner doesn't support Docker actions, `docker://` steps, or job-container `options`/`volumes`/`credentials`
(upstream limitation). Route those jobs to `dind`.

## How queueing works

1. **In Gitea:** the pod runner takes at most **2** jobs at a time and the DinD runner **1**. Further jobs stay
   *queued* in Gitea until a slot frees.
2. **In Kubernetes:** a pod job asks for **1 GiB / 0.25 vCPU** (and may grow to 2 GiB). If no node has that free,
   the job pod stays *Pending*. The job log shows "Waiting for pod … to be scheduled" and it starts as soon as
   there's room. Jobs give up only after the 3 h job timeout.
3. **AX comes first:** AX workers hold a fixed 3 GiB per node at priority `ax-workers` (5000). CI runs at
   `batch-low` (1000), so Kubernetes preempts CI pods if an AX worker needs room. Under CPU contention, AX's larger
   CPU requests win most of the CPU time. A CI job doesn't stop while an engine runs, but it is strictly second.

Roughly **2.8–3 GiB per node** is left for CI after AX, the platform and telemetry. That's about two pod jobs at
once.

## Security notes

- **Job pods** get no Kubernetes credentials. They can reach the internet, but not your LAN or anything
  cluster-internal (NetworkPolicy `ci-internet-only`).
- **The DinD runner is privileged**: a job there can break out to the node (kube-02). Use `runs-on: dind` only for
  repositories you trust, never for pull requests from forks.
- The pods runner may only manage pods (and their temporary secrets) in `gitea-runner`; it has no access to
  other namespaces.
- The registration token stays in `secrets/gitea-runner-token` (gitignored). The runners keep their
  registration on a small volume, so restarts don't re-register.

## Operating it

| Task | Command |
|---|---|
| Deploy or apply changes | `uv run just gitea-runner` |
| See runners and job pods | `kubectl -n gitea-runner get pods -o wide` |
| Runner log | `kubectl -n gitea-runner logs runner-pods-0 -c runner` (or `runner-dind-0`) |
| Change capacity or job size | edit `platform/gitea-runner/runners.yaml` (`runner-config`), then `just gitea-runner` |
| Upgrade | change the `gitea/runner:4.1.0` image tags, then `just gitea-runner` |

**You know it works when** both runners show as *online* in Gitea (Site/Org/Repo settings → Actions → Runners)
and a `runs-on: ubuntu-latest` job creates a pod in `gitea-runner` that disappears when the job finishes.
