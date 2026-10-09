# How the setup is tuned, and why it helps

**Goal:** on two 8 GiB / 4 vCPU VMs, the most memory and CPU possible should go to your jobs, and a heavy job
must never take the node, k3s or SSH down with it. Everything below serves one of those two aims. All values
are knobs in `group_data/all.py` (defaults) and can be overridden per node in `inventory.py`.

## 1. The VM itself (Proxmox)

| Setting | Why it helps |
|---|---|
| CPU type **`host`** | Exposes **AVX2/FMA** to the guest. The default type hides them, and numpy/BLAS and the JVM fall back to much slower SSE code. Also exposes nested virtualization for future micro-VMs. |
| **Ballooning device on, minimum = maximum memory** | The device still returns *unused* memory to the host (free page reporting), but Proxmox never shrinks the guest. Kubernetes sizes everything from the RAM it sees at start, and a shrinking guest causes surprise evictions. |
| VirtIO SCSI + **discard + SSD emulation**, IO thread | Thin disks shrink again (`fstrim` runs weekly); faster I/O path. |
| QEMU guest agent | Proxmox shows the node's IP; clean shutdowns. |

## 2. Memory

| What | Why it helps |
|---|---|
| **Lean NixOS** (minimal profile, no docs, polkit/udisks off) | The base OS holds only ~**115 MiB** that can't be reclaimed. |
| **zram swap, lz4, 25 % of RAM** | Idle OS and k3s pages get compressed in RAM instead of wasting it. lz4 because it's fast; numeric data compresses poorly anyway. |
| **Pods never swap** (`NoSwap`) | Swapped-out JVM heaps and numpy arrays become extremely slow. Jobs get their full RAM or are stopped cleanly; no thrashing. |
| Reclaim sysctls (`swappiness=100`, `page-cluster=0`, watermarks, `vfs_cache_pressure=50`, bounded dirty pages) | Tuned for compressed swap and steady background reclaim, avoiding stalls. Several come from your hyperland worker VM. |
| **Kubelet reservations** (256 Mi system, 256–512 Mi Kubernetes) | Pods can't eat into what the OS and k3s need. kube-01 reserves more because it also runs the control plane. |
| **Eviction before OOM** (soft 512 Mi with 90 s grace, hard 256 Mi, memcg notifications) | Under memory pressure, kubelet stops the lowest-priority pods first, *before* the kernel's OOM killer picks something at random. This plays the role earlyoom plays on a desktop. |
| **k3s protected** (`OOMScoreAdjust=-999`, CPU/IO weight 1000) | The node agent survives even when a job runs wild. |
| **One engine per worker**, 3 GiB each (Substrate's per-worker actor limit set to 1) | A busy worker is skipped, so engines always land one per node, with no collisions and no eviction risk. The remaining ~2.8–3 GiB per node goes to Gitea CI, which runs at lower priority. |

**How to check:** `just verify` asserts zram, the swap mode, the sysctls, the k3s protection, and that kubelet
sees the full RAM (it fails on a ballooned guest).

## 3. CPU

| What | Why it helps |
|---|---|
| `preempt=voluntary`, `audit=0`, soft-lockup watchdog off | More throughput for batch work; no false alarms when the Proxmox host is busy. |
| CPU mitigations **kept on** | Tasks run untrusted code; the mitigations protect the sandbox boundary. (Your desktop VM turns them off; this cluster shouldn't.) |
| BLAS/OpenMP threads = 2, `-XX:ActiveProcessorCount=2` per engine | Without this, numpy starts one thread per *host* CPU and the JVM sizes for the whole node, so jobs fight each other. Set in the example images and the handoff. |
| Batch PriorityClass `batch-low` on workers | Under pressure, batch work yields to services. |

## 4. Disk and network

| What | Why it helps |
|---|---|
| **LVM split**: `root` / `containerd` / `storage` + free space | Big images can't fill the OS disk. Container images and Substrate's actor image cache (`/var/lib/ate`, capped at 10 GiB) live on `containerd`, cleaned in steps before kubelet would evict ([guardrails](guardrails.md#disk)). Any volume can grow online. |
| `noatime`, I/O scheduler `none`, `/tmp` cleaned at boot, core dumps capped | Fewer writes; the host schedules the real disk; crashing JVMs can't fill the disk. |
| flannel **host-gw** | Both nodes share one network, so pod traffic needs no tunnel. Less CPU per packet. |
| BBR + fq, TCP Fast Open, larger socket-buffer ceilings | Better throughput for downloads and uploads. |
| **Single entry IP** (Traefik + servicelb on 10.0.0.63 only) | One address for every exposed service; DNS and clients only need that one. |

## 5. Safety rails

The full set of limits for disk, memory, CPU, objects and network, and the checklist for
anything new that grows, is in [guardrails](guardrails.md).

| What | Why it helps |
|---|---|
| gVisor sandbox per task | Untrusted task code can't reach the real kernel ([sandboxing](sandboxing.md)). |
| **Egress: public HTTPS only** | Tasks reach the internet on port 443. A NetworkPolicy on Substrate's egress gateway blocks the LAN, everything cluster-internal (AX's Redis/API, Kubernetes API, Traefik) and other ports. Verified with request/response tests. |
| Egress policy attached automatically (~0.5 s after start) | Tasks need nothing special; the gateway's "no policy yet" window is 2 s. |
| Restricted `ax-deployer` kubeconfig | Can submit and watch AX tasks; can't change or delete cluster objects or read secrets. |
| Secrets outside git and outside the Nix store | Tokens live in `secrets/` and in root-only files on the nodes. |

## 6. What it costs (measured 2026-10-04)

| Item | Memory |
|---|---|
| NixOS base, idle | ~115 MiB non-reclaimable |
| k3s server + containerd (kube-01) | ~1.2 GiB (agent on kube-02: ~0.4 GiB) |
| Observability (collectors, OTel operator, cert-manager) | ~0.35 GiB across both nodes |
| Substrate + AX control plane | ~0.5 GiB, mostly kube-01 (estimate from `kubectl top`) |
| **Left for workloads** | ~6.7–6.9 GiB allocatable per node; AX workers take 3 GiB each, CI gets the rest |

## 7. Idle (nights, weekends)

The cluster stays available at night but keeps its periodic work small:

| Trim | Effect |
|---|---|
| Telemetry: every collection and scrape every **60 s** (chart default 30 s) | fewer scrapes, including the large Kubernetes API metrics |
| Substrate API: **1 replica** (upstream 2) | one less always-polling process |
| Gitea runners: poll backoff up to **30 s** when there's no work | fewer requests to Gitea |
| Ballooning device with free page reporting | memory the VMs don't use goes back to the Proxmox host |

Measured idle (2026-10-04): **0.56 vCPU in total** (kube-01 0.34, kube-02 0.22), down from 0.66 (−15 %), of the
host's 12 threads. Actual memory in use is ~3.3 GiB; the rest is returned to the host. What remains is mostly k3s's
own control plane (~0.2 vCPU on kube-01) and Substrate's Postgres polling. Going lower would mean pausing the VMs
at night, which was deliberately not chosen: jobs at 3 a.m. should still run.

## 8. Telemetry volume (2026-10-09)

OpenObserve keeps one stream per metric name, each with its own write-ahead log, files and compaction.
Collecting "everything" produced ~554 active metric streams: Go runtime metrics from every scraped
component, CoreDNS, scheduler, controller-manager, the collectors' own metrics, histogram buckets, and
every Substrate metric twice (OTLP and Prometheus). The OpenObserve host spent its disk on small files.

Now a single allowlist (`filter/allowlist` in `platform/openobserve/values.yaml`, both collectors) lets
through only what we use:

| Kept | For |
|---|---|
| `system.*`: load, CPU, memory, filesystem, disk and network I/O | node health, the disk alerts |
| `k8s.node.*`, `k8s.pod.*`: CPU, working set, filesystem, network | per-node and per-pod use |
| `kube_*`: pod phase, restarts, waiting/terminated reasons, node conditions, replicas, requests/limits | is anything broken or stuck |
| `ate.*` over OTLP: actor CPU, crashes, image cache, worker pools, scheduling time | Substrate |
| `traces_service_graph_request_(total\|failed_total)` | the service graph |

The gateway now scrapes only kube-state-metrics (was 7 scrape jobs).

| | Before | After |
|---|---|---|
| Metric streams receiving data | ~554 | 37 |
| Rows written per 4 min | ~180,000 with the first filter, more before | ~3,700 |

To add a metric, extend the regex in `filter/allowlist` (OTTL string: write `.` as `[.]`, a
backslash escape fails the collector config) and run `uv run just openobserve`. Old streams stay
in OpenObserve until its retention removes them.
