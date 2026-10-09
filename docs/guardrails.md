# Guardrails: how the cluster protects itself

**In one sentence:** every shared resource (disk, memory, CPU, Kubernetes objects, network) has
**layered limits**. A cleaner kicks in first, then kubelet cleanup, then eviction as the last
resort. A check in `just verify` catches the problem before any of them is needed.

Read this before you add a new workload type, a new cache, or a new node.

## The rule behind every guardrail

1. **Give each tenant its own budget.** Anything that grows (caches, logs, scratch, pods) gets a
   hard cap or its own volume. "The disk is big enough" is not a budget.
2. **Clean early, evict late.** The tenant cleans up after itself first. Kubelet cleans images
   next. Evicting pods comes last, because eviction makes things worse: evicted pods are
   recreated and can be evicted again.
3. **Keep the OS volume for the OS.** Nothing that grows with workload lives on `/`.
4. **Make the failure visible before it happens.** Every guardrail has a check in
   `just verify` that fails while there is still room.
5. **Clean up the aftermath automatically.** Leftover objects (dead pods, page cache) must not
   pile up and become the next problem.

## Why: the 2026-10-05 outage

| Time (local) | What happened |
|---|---|
| 15:30–17:05 | OpenUltraSAST rebuilt its task image 5 times, about 1.4 GiB of new layers each time. Every node unpacked every version into Substrate's image cache (`/var/lib/ate/image-cache`). |
| ~17:50 | The cache reached 14 GiB (18 versions) on the 20 GiB OS volume, with 1.5 GiB free. Kubelet's `nodefs.available < 10%` limit fired. |
| 17:50–18:05 | Kubelet evicted Substrate, AX and telemetry. `kube-state-metrics` was recreated and evicted again about **3,000 times**. Both kubelets ran at ~300% CPU on the dead-pod backlog. The telemetry gateway was killed for running out of memory. |
| 18:05–18:15 | Fix: cache moved to the containerd volume, cache capped, dead pods deleted, k3s restarted. Everything back; 46 verify checks pass. |

**What failed:**

- one tenant (the image cache) had no budget;
- it shared the OS volume;
- nothing warned while there was still room;
- the eviction aftermath fed itself.

All four are fixed below. The same pattern applies to any future tenant.

## Disk

Each node has an LVM volume group `pool`. Every volume has one job:

| Volume | Holds | Who keeps it in check |
|---|---|---|
| `root` 20G (`/`) | NixOS, journal, k3s server state | journal capped at 200M, core dumps at 500M, `/tmp` cleaned at boot. **No workload data.** |
| `containerd` 35G | container images **and** `/var/lib/ate` (Substrate's actor image cache and task `/workspace`), via bind mount | see the cleanup order below |
| `storage` 30G | persistent volumes (local-path), e.g. Substrate's Postgres | sizes declared per volume claim |
| free space in `pool` | headroom | `lvextend -r` grows any volume online |

**Cleanup order on the containerd volume.** Each step fires before the next one is needed:

| Disk use | Who acts | What happens |
|---|---|---|
| any time cache > 10 GiB | atelet (`--image-cache-max-bytes`) | deletes unused actor image layers, oldest first |
| 75% | atelet (`--image-cache-high-percent=75`, low 65) | deletes more cached layers until 65% |
| 80% | kubelet image cleanup (`image_gc_high=80`, low 60) | deletes unused container images until 60% |
| 85% (15% free) | kubelet eviction (`imagefs.available`) | **last resort:** evicts pods |

**Budget on the 35G containerd volume** (per node): container images ~4 GiB + actor image cache
≤ 10 GiB + one task's `/workspace` ≤ 2 GiB = ~16 GiB, below the 75% (26 GiB) mark where cleanup
starts. The 10 GiB fits OpenUltraSAST's rotation (engine ~5 GB + search 2.1 GB unpacked, replay
shares the engine's layers). If tenants need more, grow the volume (`lvextend -r`) before raising the cap.

On `/`, kubelet evicts at 10% free (`nodefs.available`). Since nothing grows there any more, this
should never fire.

**Settings live in:**

- `group_data/all.py`: `disk_layout`, `image_gc_*`, `eviction_hard`, `ate_on_containerd`;
- `nix/modules/k3s.nix`: the bind mount;
- the atelet flags in `platform/substrate/kube-ax-edits.py` (applied to the pinned Substrate checkout).

## Memory

| Guardrail | Effect |
|---|---|
| `system_reserved` / `kube_reserved` 256 Mi each | the OS and k3s keep their share; pods can't schedule into it |
| Eviction: soft at 512 Mi free (90 s grace), hard at 256 Mi free | kubelet stops the lowest-priority pod before the kernel's out-of-memory killer picks a random one |
| k3s `OOMScoreAdjust=-999` | the out-of-memory killer never picks k3s |
| Go memory caps: k3s server 800 MiB, agent 256 MiB, atelet 160 MiB (`GOMEMLIMIT`, `GOGC=50`) | the cluster's own processes give memory back instead of growing |
| AX workers: request = limit (3 GiB) | a task can't take more than its worker; the worker's memory is always really there |
| Memory limits on AX workers, CI jobs, Postgres, telemetry (plus its `memory_limiter`) and the reaper | a misbehaving component is restarted alone |
| `/tmp` inside a task is RAM; `/` and `/workspace` are disk ([sandboxing](sandboxing.md#5-where-a-tasks-files-live-measured-2026-10-06)) | task authors set `TMPDIR=/workspace/tmp`, so big temp files don't eat the worker's 3 GiB |
| zram swap, balloon min = max in Proxmox | short spikes get compressed instead of killing; the VM never shrinks under a job |
| Nightly clean page-cache drop (03:30) | dashboards show real usage, not cache left by deleted pods |

## CPU and scheduling

| Guardrail | Effect |
|---|---|
| PriorityClasses: `ax-workers` 5000 > services > `batch-low` 1000 | under pressure, CI and batch work make room for AX first |
| AX: one task per worker, one worker per node | a task can't starve its neighbour; the most tasks running at once is known (2) |
| CI runner capacity (2 pod jobs, 1 Docker-in-Docker job) plus 1 GiB requests | CI waits in Gitea or as a Pending pod instead of overloading a node |
| `system_reserved` / `kube_reserved` 200m CPU each | kubelet and the OS stay responsive at 100% load |

## Kubernetes objects

| Guardrail | Effect |
|---|---|
| `terminated-pod-gc-threshold=100` (default 12,500) | at most 100 failed or evicted pods are kept; an eviction loop can't leave thousands behind |
| AX reaper (`tools/ax-reaper`) | `ousast-engine-*` tasks are deleted 10 min after their command exits, so finished tasks don't hold workers |
| Only the extensions (CRDs) that are actually used | every installed extension costs API-server memory |

## Network

| Guardrail | Effect |
|---|---|
| gVisor sandbox per task | task code never talks to the real kernel |
| Egress: public HTTPS only (NetworkPolicy on Substrate's egress gateway) | tasks can't reach the LAN, the cluster's internal services or other ports |
| CI job pods: internet only | CI can't reach the LAN or the cluster's internals |

## Alerts: OpenObserve to Discord

OpenObserve checks every 5 minutes and posts to Discord, mentioning the operator:

| Alert | Fires when | Repeats at most |
|---|---|---|
| `kube-ax-disk-free-below-25pct` | `/`, the containerd or the storage volume of a node has < 25 % free (10-min average) | hourly |
| `kube-ax-disk-free-below-15pct` | same, < 15 % (kubelet evicts at 10 % on `/`, 15 % on containerd) | every 30 min |
| `kube-ax-node-pressure` | a node reports disk or memory pressure, or ≥ 20 pods were evicted in 10 min | every 30 min |

Single evictions don't alert: they are kubelet doing its job. A storm (the 2026-10-05 outage evicted
~3000 pods) does.

- Set up and changed with `uv run python scripts/openobserve-alerts.py apply` (re-runnable).
- `discover` shows the disk metrics; `e2e` creates a throwaway alert that must fire, waits for it, and
  deletes it (verified 2026-10-09: message arrived in Discord).
- Secrets (gitignored): `secrets/openobserve-admin` (login with alert rights), `secrets/discord-webhook`,
  `secrets/discord-user-id`.
- Data: disk from the `hostmetrics` filesystem scraper (`k3s_system_filesystem_usage`, node in
  `k8s_node_name`); events from `k3s_events` (`body_object_reason`).

## Checks: `uv run just verify`

These checks fail **while there is still room to act**:

| Check | Fails when |
|---|---|
| `/` and the containerd volume ≥ 25% free | a tenant is growing; act before the 75/80/85% cleanup steps start |
| actor image cache ≤ 11 GiB | the 10 GiB cap isn't working |
| `/var/lib/ate` on the containerd volume | the bind mount is missing (e.g. a node was rebuilt without it) |
| MemTotal matches the VM size | the Proxmox balloon shrank a node |
| kube-system pods healthy | something is crash-looping or was evicted |

Run it after every change and after anything unusual. It takes about 2 minutes.

## Checklist: adding something that grows

Use this for a new cache, log, queue, database or workload type:

1. **Where does it write?** Not `/`. Pick the containerd volume (images, scratch) or a
   volume claim on `storage` (data that must survive).
2. **What is its cap?** A size flag, a volume claim size, a retention period, or a capacity
   setting. Write the number down in `group_data/all.py` or the manifest, with a comment saying why.
3. **Who cleans it, and at what threshold?** It must act before kubelet: below 80% on the
   containerd volume.
4. **What if it misbehaves?** A memory limit, a PriorityClass below `ax-workers`, and a
   restart that doesn't take others down.
5. **How do we see it early?** Add a check to `deploys/verify.py`.
6. **What does it leave behind?** Finished jobs, dead pods, old snapshots: name the thing that
   deletes them.

## Not covered yet (honest list)

- **Event collection can stall.** After the 2026-10-05 outage the telemetry gateway stopped shipping
  Kubernetes events for 3.5 days (logs kept flowing) until it was restarted on 2026-10-09. The
  node-pressure alert depends on those events; the disk alerts don't. No alert watches for the gap yet.
- **No alert on pod restart rate.**
- **No per-namespace quotas** (ResourceQuota / LimitRange), and several platform pods have no memory
  limit: Substrate's API server, controller, atelet, router and egress gateway; cert-manager;
  Traefik; metrics-server; the OpenTelemetry operator. Only eviction holds them back.
- **No PID limit per pod.** A fork bomb outside gVisor (e.g. in CI) could exhaust node PIDs.
- **The telemetry gateway still has a fixed 384 Mi limit.** It holds steady normally but ran
  out of memory during the eviction storm (too many object changes at once). It restarts by
  itself and loses only telemetry.
- **We don't know why atelet's 85% watermark cleaner didn't fire before the outage.** Its logs
  were lost with the evicted pods. The 10 GiB hard cap now covers it either way.

**You know the guardrails work when** `uv run just verify` passes and, after a day of normal
task-image churn, the actor image cache stays at or below about 10 GiB per node.
