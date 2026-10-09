# Requirements Document

## Introduction

This feature delivers a small, low-overhead Kubernetes (k3s) cluster on NixOS VMs, driven entirely by
**pyinfra** from the operator's workstation. It covers two phases per node:

1. **Install phase** – take a VM booted from the NixOS live ISO (SSH enabled, user `nixos`, passwordless
   sudo), partition the attached disk, run `nixos-install`, and reboot into the installed system.
2. **Configure phase** – on the installed system, enable k3s through the native NixOS `services.k3s`
   module and join the node to the cluster as either the server (control plane + workloads) or an agent.

The first node (`10.0.0.63`) becomes the k3s server **and** runs workloads. It is also the cluster's
single entry point: services are exposed on its IP. A second node is added as an agent (the operator will
supply its IP), and future nodes are added the same way: add an inventory entry, run install, run
configure.

The cluster is intentionally small. **Memory and CPU efficiency is the primary design goal**: everything
not doing the operator's work (OS, Kubernetes, platform) must be measured and kept small, and the knobs
that trade overhead for capability must be explicit and documented. HA and feature completeness come
second.
Workloads are spread horizontally across nodes for capacity, not for redundancy, and no HA replicas are
required. Metrics will later be shipped to OpenObserve (separate spec). This spec only has to keep that
path open.

### Observed environment (probed 2026-10-04 from 10.0.0.63)

| Item | Value |
|---|---|
| OS | NixOS live ISO 26.05 (Yarara), `26.05.11006.4feb8eb8bf30` |
| CPU / RAM | 3 vCPU / ~5.9 GiB, no swap |
| Target disk | `/dev/sda`, 100 GiB, empty (no partition table) |
| Boot mode | **BIOS** (legacy) — no `/sys/firmware/efi` |
| NIC | `ens18` (virtio, Proxmox-style), MAC `bc:24:11:a3:c7:ba` |
| Network | `10.0.0.63/24` via DHCP, gateway `10.0.0.1`, DNS `10.0.0.1` |
| Access | SSH key auth as `nixos`, `sudo -n` works |
| CPU model | Intel Core i7-8700 (Coffee Lake) via Proxmox CPU type `host`: AVX2 + FMA yes, AVX-512 no. (The default `QEMU Virtual CPU` type, used initially, exposed only SSE4.2) |
| Virtualisation | Nested enabled on the Proxmox host (`kvm_intel nested=Y`); guest has `vmx`, `/dev/kvm`, `kvm_intel` loaded |
| Kernel / cgroups | 6.18.54, cgroup v2 |

## Requirements

### Requirement 1 — Reproducible operator tooling

**User Story:** As an operator, I want a pinned, reproducible pyinfra environment in this repo, so that
any workstation can run the same deploys with the same results.

#### Acceptance Criteria

1. WHEN the operator runs the documented bootstrap command (`just setup` → `uv sync`) THEN the system SHALL
   create a project-local uv virtual environment at `./.venv` with the Python version pinned in
   `.python-version` and pyinfra 3.x locked in `uv.lock`.
2. All deploy entry points SHALL run inside that venv (`uv run pyinfra …`); no global/system pyinfra
   install SHALL be required or used. `.venv/` SHALL be gitignored. The task runner `just` SHALL come
   from the venv too (PyPI package `rust-just`), so the workstation needs only `uv`, `git`, `go`, `kubectl`
   and `podman`.
2a. The workstation has **no Nix**. Nix evaluation checks SHALL run in a pinned `nixos/nix` container via
   podman. All real builds happen on the nodes (`nixos-install`, `nixos-rebuild`).
3. WHEN the operator lists available tasks THEN the system SHALL expose named entry points for at least
   `install`, `k3s`, `platform`, `verify`, and `kubeconfig`.
4. The repository SHALL pin nixpkgs (and any other Nix inputs) via `flake.lock` so that every node builds
   from the same package set.
5. The repository SHALL NOT contain secrets in tracked files; secret material SHALL live in a gitignored
   `secrets/` directory.

### Requirement 2 — Inventory as the single source of truth

**User Story:** As an operator, I want every node described in one inventory file, so that adding a node
is a data change and not a code change.

#### Acceptance Criteria

1. The inventory SHALL define for each node: address, node name (hostname), k3s role (`server` | `agent`),
   install disk, and boot mode (`bios` | `uefi`). The address is the IP the node received from DHCP while
   booted from the ISO. Prefix, gateway, and DNS are optional overrides; by default they are taken from
   the host's DHCP lease at install time (see Requirement 4).
2. The inventory SHALL support exactly one `server` node in the initial version.
3. WHEN a node lacks a required inventory field THEN the deploy SHALL fail before making any change, with
   an error naming the missing field and the host.
4. WHEN Nix configuration is rendered for a node THEN it SHALL be derived only from inventory data and
   facts collected from that host (no hand-edited per-host Nix files).

### Requirement 3 — Unattended OS install from the live ISO

**User Story:** As an operator, I want to install NixOS onto a freshly booted ISO VM with one command, so
that new nodes are provisioned identically and without manual steps.

#### Acceptance Criteria

1. WHEN the install deploy runs against a host THEN the system SHALL first verify that the host is running
   the NixOS live ISO and SHALL abort otherwise.
2. WHEN the configured install disk already contains a partition table or filesystem THEN the system SHALL
   abort unless the operator explicitly passes a wipe confirmation for that host.
3. WHEN the detected boot mode differs from the inventory boot mode THEN the system SHALL abort with a
   clear error.
4. WHEN preconditions pass THEN the system SHALL partition, format, and mount the disk declaratively
   (disko), matching the boot mode (BIOS: GPT + BIOS boot partition + GRUB; UEFI: GPT + ESP +
   systemd-boot). The rest of the disk SHALL be one LVM volume group `pool` with ext4 (`noatime`) logical
   volumes `root` (`/`), `containerd` (`/var/lib/rancher/k3s/agent/containerd`, the kubelet imagefs) and
   `storage` (`/var/lib/rancher/k3s/storage`, local-path volumes). Sizes come from the `disk_layout` knob
   (default 20G / 35G / 30G), and the remaining extents stay free for online growth (`lvextend -r`).
5. WHEN the disk is mounted THEN the system SHALL generate the hardware configuration on the host,
   install the repo's flake configuration with `nixos-install`, and SHALL NOT set a root password.
6. The installed system SHALL contain an admin user with the operator's SSH public key(s), passwordless
   sudo for that user, and SSH password authentication disabled.
7. WHEN installation succeeds THEN the system SHALL reboot the host and wait until SSH on the installed
   system is reachable, then report success.
8. The install phase SHALL install the base OS only; k3s SHALL NOT be enabled until the configure phase.
9. The hand-off contract between operator and pyinfra SHALL be: a Proxmox VM with pre-assigned hardware,
   booted from a NixOS ISO, reachable by SSH as `nixos` at the inventory address. Nothing beyond this
   SHALL be done by hand.
10. The **default** path SHALL be the stock NixOS ISO mounted on the VM. The documented manual hand-off is:
    `passwd` on the console, then `ssh-copy-id nixos@<ip>` from the workstation. Only the operator's
    **public** key is copied; private keys never leave the workstation. WHEN pyinfra cannot log in with the
    key THEN install SHALL fail before any change, and its error SHALL show these two commands.
11. *(Optional, later)* The repository MAY build a custom installer ISO (`just iso`) with the operator's
    SSH keys, `sshd` and the QEMU guest agent pre-enabled. Without local Nix it builds in the podman Nix
    container or on an installed node.
12. The operator documentation SHALL specify the Proxmox VM settings: boot order disk before CD-ROM (an
    empty disk falls through to the ISO; an installed disk boots directly, so the ISO need not be
    ejected), CPU type `host`, ballooning off, VirtIO SCSI with discard, virtio NIC.
13. WHEN the install deploy runs THEN it SHALL fail before touching the disk unless the host CPU exposes
    `avx2`, `fma` and `vmx` or `svm`, and `/dev/kvm` exists. SSE-only CPUs and nodes without nested
    virtualisation are not supported (Requirements 13.14, 16.6).

### Requirement 4 — Low-footprint base system

**User Story:** As an operator, I want the base NixOS install to be lean, so that most of the VM's RAM and
CPU is available to workloads.

#### Acceptance Criteria

1. WHEN the install deploy runs THEN the system SHALL read the host's current DHCP-assigned address,
   prefix, gateway, and DNS servers from the live ISO, and SHALL fail if the address differs from the
   inventory address.
2. The installed system SHALL configure those values **statically** on the primary NIC, with no DHCP
   client, so the node's IP never changes after install.
3. The captured network values SHALL be stored per host in the repo (`host_network/<node_name>.json`).
   The configure phase SHALL reuse them, so later runs do not depend on the ISO.
4. The operator documentation SHALL instruct reserving or excluding each node IP in the DHCP server, to
   prevent the address being leased to another device.
5. The base system SHALL disable documentation, GUI components, and services not needed for a headless
   Kubernetes node.
6. The base system SHALL cap journald disk usage and enable automatic Nix garbage collection and store
   optimisation.
7. The base system SHALL load the kernel modules and sysctls required by k3s networking. Swap and host
   tuning are covered by Requirement 13.
8. WHEN the base system is idle (before k3s) THEN its memory use SHOULD stay below 250 MiB.

### Requirement 5 — k3s server via the NixOS module

**User Story:** As an operator, I want the first node configured as the k3s server through
`services.k3s`, so that the control plane is declarative and upgraded with the rest of the OS.

#### Acceptance Criteria

1. WHEN the configure deploy runs against a `server` node THEN the system SHALL enable `services.k3s` with
   `role = "server"` and apply it with `nixos-rebuild switch`.
2. The server SHALL use the embedded SQLite datastore (no `--cluster-init`, no embedded etcd) to minimise
   overhead.
3. The server node SHALL NOT be tainted, so that it schedules regular workloads.
4. The server SHALL advertise the inventory IP as its node IP and API address.
5. WHEN `nixos-rebuild switch` completes THEN the system SHALL wait until the node reports `Ready` before
   continuing to agent nodes.

### Requirement 6 — k3s agents join the cluster

**User Story:** As an operator, I want additional nodes to join as agents automatically, so that the
cluster grows by adding inventory entries.

#### Acceptance Criteria

1. WHEN the configure deploy runs against an `agent` node THEN the system SHALL enable `services.k3s` with
   `role = "agent"`, `serverAddr` pointing to the server node, and the shared cluster token.
2. Agent configuration SHALL only be applied after the server is `Ready`.
3. WHEN an agent has been configured THEN it SHALL appear as `Ready` in `kubectl get nodes` within a
   bounded timeout (default 5 minutes), or the deploy SHALL fail with diagnostic output.

### Requirement 7 — Low-overhead k3s profile

**User Story:** As an operator, I want k3s tuned for a small cluster, so that it uses little RAM and CPU.

#### Acceptance Criteria

1. The k3s configuration SHALL allow disabling bundled components through a configurable list. The
   default keeps `coredns`, `local-storage`, `metrics-server`, `servicelb`, and `traefik`: metrics-server
   is needed for later observability, and servicelb + traefik provide the single entry IP
   (Requirement 11).
2. The k3s configuration SHALL disable the cloud controller.
3. WHEN all nodes share one L2 subnet THEN flannel SHALL use the `host-gw` backend (no VXLAN encapsulation);
   otherwise it SHALL fall back to `vxlan`.
4. Kubelet SHALL reserve resources for the system and Kubernetes (`system-reserved`, `kube-reserved`) and
   set a memory hard-eviction threshold, using configurable defaults sized for small VMs.
5. Kubelet image garbage collection thresholds SHALL be set so that the node disk does not fill up.
6. WHEN the cluster is idle THEN the server node's k3s process SHOULD use less than 700 MiB RSS and an agent
   node's k3s process less than 300 MiB RSS.

### Requirement 8 — Workloads distributed horizontally across nodes

**User Story:** As an operator, I want workloads spread across all VMs, so that the CPU and RAM of every
node is used. I do not need HA replicas.

#### Acceptance Criteria

1. All nodes, server included, SHALL be schedulable for regular workloads.
2. The cluster SHALL NOT require more than one replica per workload; distribution comes from the
   scheduler placing different workloads on different nodes.
3. Every node SHALL carry consistent topology labels (`kubernetes.io/hostname`, plus a
   configurable `topology.kubernetes.io/zone`, default one zone per node) so workloads can use
   `topologySpreadConstraints`.
4. WHEN the verify task runs THEN it SHALL deploy a test Deployment with one replica per node and a
   hostname topology spread constraint, assert that pods land on every Ready node, and then remove it.

### Requirement 9 — Secrets handling

**User Story:** As an operator, I want the cluster token and every credential kept out of Git and the
Nix store, and the kubeconfig generated locally, so that the repository can be public.

*Changed 2026-10-06: the repository goes public. Kubeconfigs were committed in plaintext while it was
private (criteria 4–6, superseded); they are now local only.*

#### Acceptance Criteria

1. WHEN no cluster token exists locally THEN the system SHALL generate a random token into
   `secrets/k3s-token` (mode 0600).
2. The token SHALL be uploaded to each node outside the Nix store (root-owned, mode 0600) and referenced
   through `services.k3s.tokenFile`.
3. WHEN the operator runs the `kubeconfig` task (also run at the end of `k3s` for the server) THEN the
   system SHALL fetch `/etc/rancher/k3s/k3s.yaml` from the server, rewrite the server URL to
   `https://<server ip>:6443`, rename cluster/context/user from `default` to the cluster name
   (default `kube-ax`), and write it to `kubeconfig/<cluster>.yaml` (mode 0600).
4. `kubeconfig/` SHALL be gitignored (except its README); the system SHALL NOT commit kubeconfigs.
5. Documents that need the ax-deployer kubeconfig inline (AX guide, handoff brief) SHALL keep a
   placeholder in git; `just ax-handoff` renders them with the real kubeconfig into the gitignored
   `.cache/handoff/`.
6. gitleaks SHALL run without an allowlist on every commit.
7. `secrets/` (k3s token, ghcr token) and `kubeconfig/` SHALL remain gitignored. A pre-commit check SHALL
   fail any commit that adds files under either (except their READMEs).

### Requirement 10 — Idempotency, re-runs, and adding future nodes

**User Story:** As an operator, I want every deploy to be safe to re-run and to add nodes the same way, so
that day-2 changes and growth are routine.

#### Acceptance Criteria

1. WHEN the configure deploy is re-run with no inventory or Nix changes THEN it SHALL report no changes to
   the system configuration and SHALL NOT restart k3s.
2. WHEN the install deploy targets a host that is already installed (not the live ISO) THEN it SHALL skip
   that host with a clear message instead of failing the whole run.
3. WHEN a new node is added to the inventory THEN running `install` then `k3s` limited to that node SHALL
   join it to the cluster without changing existing nodes.
4. WHEN the shared Nix configuration changes THEN running `k3s` across all nodes SHALL roll the change out
   with agents updated one at a time after the server.

### Requirement 11 — Single entry IP for services

**User Story:** As an operator, I want all exposed services reachable through one IP, the server
node's, so that clients and DNS records only need to know that one address.

#### Acceptance Criteria

1. `LoadBalancer` Services SHALL be published only on the server node's IP. servicelb SHALL be
   restricted to the server node (node label `svccontroller.k3s.cattle.io/enablelb=true` on the server
   only), and agents SHALL NOT expose LoadBalancer ports.
2. The bundled Traefik ingress controller SHALL run as a single replica pinned to the server node, and be
   exposed on ports 80/443 of the server IP.
3. WHEN a request reaches the server IP for a service whose pods run on an agent THEN the cluster SHALL
   route it to that pod (via kube-proxy/flannel) without extra configuration.
4. WHEN the verify task runs THEN it SHALL deploy a test Ingress whose backend pod is pinned to an agent
   node (when one exists), assert an HTTP 200 through the server IP, and then remove it.
5. The server node firewall SHALL allow TCP 80 and 443. Agents SHALL NOT open them.

### Requirement 12 — Observability readiness (OpenObserve, later)

**User Story:** As an operator, I want the cluster ready for shipping metrics to OpenObserve later, so
that adding observability needs no rework of the base install.

#### Acceptance Criteria

1. metrics-server SHALL be enabled so `kubectl top` and the resource metrics API work.
2. Kubelet/cAdvisor metrics SHALL remain reachable from inside the cluster (port 10250 open between
   nodes), so a future collector DaemonSet (e.g. OpenTelemetry Collector) can scrape them.
3. Node-level settings (journald retention, resource reservations) SHALL leave room for one lightweight
   collector pod per node. The design SHALL state the reserved headroom.
4. ~~Out of scope~~ Done early at the operator's request (2026-10-04): `just openobserve` installs the
   OpenObserve collector stack (cert-manager v1.20.0, Prometheus-operator CRDs v0.77.1, OpenTelemetry
   operator 0.138.0, chart `openobserve/openobserve-collector` 0.5.0), equivalent to
   `o2-datasource/k8s/install.sh`, with a sizing overlay (`platform/openobserve/values.yaml`): agent
   DaemonSet ≤ 256 MiB, gateway ≤ 384 MiB, `memory_limiter` at ~80% of each. Target
   `http://10.0.0.54:40002`, org `default`, cluster `kube-ax`; ingestion key in
   `secrets/openobserve-auth`.
5. Auto-instrumentation `Instrumentation` resources exist for Go, Java, Python, Node.js and .NET. Namespaces
   opt in by annotation. `ax-system` (and `ate-system`) SHALL be annotated for Go once AX/Substrate are
   installed, with `otel-go-auto-target-exe` set to the ko binary path (`/ko-app/<name>`).

### Requirement 13 — Host tuning for CPU- and memory-intensive jobs

**User Story:** As an operator, I want the Linux host tuned for heavy batch jobs, with compressed swap in
RAM, sensible VM/network sysctls, and protected system daemons, so that heavy jobs use the hardware fully
without destabilising the node or the control plane.

#### Acceptance Criteria

1. Swap SHALL be selectable per host with `swap_mode = "zram" | "zswap" | "none"`. The default is `zram`,
   using the fast `lz4` algorithm (low latency, lower ratio) sized to 25% of RAM. When the pinned kernel and
   zram tooling support it, idle pages MAY be recompressed with `zstd`. `zswap` mode uses a compressed RAM
   cache in front of a small swap file, so cold pages go to disk. No disk-backed swap is used in `zram` mode.
2. Swap is a safety net for OS, k3s and idle pages, **not extra capacity for workloads**. Kubelet SHALL be
   configured with `failSwapOn: false` and `memorySwap.swapBehavior`, default `NoSwap`, so pods do not
   swap. That matters because JVM garbage collection and numpy/pandas arrays degrade badly when swapped.
   `LimitedSwap` (Burstable pods only) SHALL be selectable per host.
3. VM sysctls SHALL be tuned for compressed swap (`vm.swappiness`, `vm.page-cluster=0`,
   `vm.watermark_boost_factor=0`, `vm.watermark_scale_factor=125`, a `vm.min_free_kbytes` floor, bounded
   dirty-page writeback via `vm.dirty_background_bytes`/`vm.dirty_bytes`). MGLRU thrashing protection
   (`/sys/kernel/mm/lru_gen/min_ttl_ms`) SHALL be enabled, so the node evicts or OOM-kills instead of
   thrashing.
4. Kernel sysctls SHALL match what kubelet expects (`vm.overcommit_memory=1`, `vm.panic_on_oom=0`,
   `kernel.panic=10`, `kernel.panic_on_oops=1`).
5. Limits for container-dense nodes SHALL be raised: `fs.inotify.max_user_instances`,
   `fs.inotify.max_user_watches`, `fs.file-max`, `vm.max_map_count`, `kernel.pid_max`, and
   `net.netfilter.nf_conntrack_max`.
6. Network sysctls SHALL be tuned: `net.core.default_qdisc=fq`, `net.ipv4.tcp_congestion_control=bbr`,
   larger `net.core.somaxconn` and backlog, and a widened `net.ipv4.ip_local_port_range`.
7. Transparent huge pages SHALL be set to `madvise` (defrag `defer+madvise`). numpy already calls
   `madvise(MADV_HUGEPAGE)` for large arrays, and the JVM can opt in with `-XX:+UseTransparentHugePages`,
   so heavy jobs get huge pages without latency spikes for everything else.
8. The k3s service SHALL be protected from heavy workloads: `OOMScoreAdjust=-999`, a higher `CPUWeight`
   and `IOWeight` than `kubepods.slice`, and kubelet `enforce-node-allocatable=pods`, so pods cannot eat
   into reserved resources.
9. `systemd-oomd` and `earlyoom` SHALL NOT run on nodes. Kubelet eviction is the node's earlyoom
   equivalent, and a second userspace killer would kill pods without kubelet knowing.
10. Kubelet SHALL use soft and hard memory-eviction thresholds (soft with a grace period), so memory
    pressure evicts pods before the kernel OOM killer fires. Pods are evicted in this order: BestEffort,
    then Burstable over request, then lowest PriorityClass. Kubelet SHALL set
    `kernelMemcgNotification: true`, so eviction reacts to kernel memory-cgroup threshold events
    immediately instead of waiting for the ~10 s housekeeping poll.
11. The kubelet CPU manager policy SHALL be configurable (`none` default; `static` optional, with
    `reserved-cpus`), so CPU-bound Guaranteed pods can get exclusive cores on hosts with enough vCPUs.
12. Filesystems SHALL mount with `noatime`, and periodic `fstrim` SHALL be enabled (thin-provisioned
    virtual disks). Virtual disks SHALL use the `none` I/O scheduler (the Proxmox host schedules the real
    disks). `/tmp` stays on disk (no RAM cost) and is cleaned on boot. Core dumps are capped
    (`MaxUse=500M`, `KeepFree=1G`).
12a. Settings carried over from the operator's tuned Proxmox desktop VM (`hyperland-forky-worker-vm`):
    `vm.vfs_cache_pressure=50`, `vm.dirty_writeback_centisecs=1500`, TCP Fast Open, autotuned 128 MB
    socket-buffer ceilings, `tcp_mtu_probing`, `tcp_no_metrics_save`, `netdev_budget=600`, `audit=0`,
    soft-lockup watchdog off (knob), floppy/pcspkr/joydev blacklisted, `DefaultLimitNOFILE=65536:524288`
    (knob), and haltpoll cpuidle as a knob (default off, because it spends host CPU shared with other
    VMs). Not carried over: `mitigations=off` (untrusted agent code runs here) and earlyoom (kubelet
    eviction replaces it).
13. All tuning values SHALL live in one NixOS module, with defaults overridable per host through
    inventory data (`node.json`).
14. The operator documentation SHALL list the hypervisor-side settings that the node cannot set itself.
    Memory ballooning: keep the **Ballooning Device enabled** (free page reporting returns idle guest
    memory to the host) but set **Minimum memory = Memory**, so Proxmox never inflates the balloon.
    Inflation shrinks the guest under running pods, while kubelet sizes allocatable and eviction from
    the RAM it saw at start (operator decision 2026-10-04). `just verify` SHALL fail when a node's
    MemTotal is below `node_memory_gib` or when kubelet's capacity lags MemTotal (restart k3s).
    CPU type `host` is **mandatory**: the default type hides AVX/AVX2/FMA, and SSE-only code paths for
    numpy/OpenBLAS and the JVM are not acceptable. `host` also passes through vmx/svm for nested
    virtualisation. Also:
    memory ballooning disabled, virtio-scsi + discard + IO thread on disks, and nested virtualisation
    enabled on the Proxmox host (`kvm_intel nested=1` / `kvm_amd nested=1`) for micro-VM sandboxes
    (Requirement 16.6).
15. The workload guide SHALL explain the layered OOM model, from first to last line of defence:
    container cgroup limit (container `OOMKilled`) → kubelet soft/hard eviction (pod `Evicted`) → MGLRU
    thrash protection → kernel global OOM killer, guided by kubelet's QoS `oom_score_adj` (Guaranteed
    −997, Burstable 2–999, BestEffort 1000) with k3s at −999.
16. WHEN the verify task runs THEN it SHALL assert on each node that zram is active, that the key sysctls
    have their expected values, and that the k3s unit has the expected `OOMScoreAdjust`. It SHALL **fail**
    when `avx2`/`fma` are missing from `/proc/cpuinfo` or `/dev/kvm` is missing.

### Requirement 14 — Container images from GitHub Container Registry

**User Story:** As an operator, I want every node to pull images from GitHub's registry (`ghcr.io`),
including private ones, without running a separate registry or mirror in the cluster.

Context: the operator uses a GitHub Pro **user** account with per-repository packages. Images are named
`ghcr.io/<ghcr_owner>/<image>` and linked to their source repository through the OCI label
`org.opencontainers.image.source`, so each package inherits that repo's visibility and access.

#### Acceptance Criteria

1. The cluster SHALL NOT run an in-cluster registry or pull-through mirror.
2. WHEN `secrets/ghcr-token` exists (a **classic** PAT with `read:packages`; GitHub Packages did not
   accept fine-grained PATs for the container registry at the time of writing, so re-check) THEN the configure deploy SHALL
   write node-level registry credentials for `ghcr.io` to `/etc/rancher/k3s/registries.yaml` (root 0600,
   outside the Nix store) on every node, so pods can pull private images without per-namespace
   `imagePullSecrets`.
3. WHEN the token or registry settings change THEN the deploy SHALL restart k3s on that node so that
   containerd reloads them. WHEN they have not changed THEN it SHALL NOT restart k3s.
4. WHEN `secrets/ghcr-token` is absent THEN the deploy SHALL skip registry credentials, so only public
   images can be pulled, and SHALL print a warning.
5. Kubelet image garbage collection (Req 7.5) SHALL be sized for large data-science and JVM images
   (multi-GB layers). The verify task SHALL report image filesystem usage per node.
6. WHEN the verify task runs and a ghcr token is configured THEN it SHALL pull a configured test image from
   `ghcr.io` on every node.

### Requirement 15 — Workload guidance for numpy/pandas and Java jobs

**User Story:** As a developer running Python (numpy/pandas) and Java jobs on this cluster, I want clear
defaults for sizing and threading, so that jobs use the node's CPU and RAM efficiently without throttling,
oversubscription, or OOM surprises.

#### Acceptance Criteria

1. The repository SHALL include a workload guide (`docs/workloads.md`) and example manifests
   (`examples/`) covering:
   - Python: setting `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS` and
     `NUMEXPR_NUM_THREADS` to the pod's CPU request, because BLAS otherwise spawns one thread per host CPU;
     and pandas memory reduction (downcasting dtypes, `category`, Arrow-backed dtypes, chunked reads).
   - Java: container-aware heap sizing (`-XX:MaxRAMPercentage`, leaving headroom for off-heap/metaspace),
     `-XX:ActiveProcessorCount` matched to the CPU request, collector choice (G1 default; ZGC for large
     heaps), and optional `-XX:+UseTransparentHugePages`.
   - Resources: memory `requests == limits` for JVM and large-array jobs; CPU requests without CPU limits
     for batch jobs (avoids CFS throttling); `Guaranteed` QoS plus integer CPUs when the static CPU
     manager is enabled.
   - Batch scheduling: `Job` with `backoffLimit`, `ttlSecondsAfterFinished`, and a low-priority
     `PriorityClass`, so batch work yields to services under pressure.
2. The configure deploy SHALL create two PriorityClasses, `batch-low` and `service-default`, through
   the server manifests directory.
3. The verify task SHALL NOT depend on these guidelines; they are documentation and examples only.

### Requirement 16 — AX on Agent Substrate (agent runtime platform)

**User Story:** As an operator, I want [AX](https://github.com/google/ax) running on the cluster, so that I
can run sandboxed autonomous-agent workloads alongside regular jobs.

AX is an orchestrator, not a container runtime. It schedules every task as a sandboxed actor on
[Agent Substrate](https://github.com/agent-substrate/substrate) (namespace `ate-system`), which must be
installed first. Substrate runs actors in **gVisor** (`runsc`, default, a general-purpose user-space kernel
sandbox that needs no KVM) or **micro-VMs** (Kata + Cloud Hypervisor, needs `/dev/kvm`). The sandbox
binaries are not installed on nodes: the `atelet` DaemonSet fetches them from a cluster-scoped
`SandboxConfig`, and `ateom` runs them inside worker pods.

#### Acceptance Criteria

1. The node container runtime SHALL be k3s's embedded containerd with the default `runc` handler, for both
   regular pods and Substrate's worker pods. Nodes SHALL NOT need a `runsc` or Kata RuntimeClass/handler,
   because Substrate supplies sandbox binaries via `SandboxConfig`.
2. Nodes SHALL set the sysctls Substrate needs for gVisor pod networking: `net.ipv4.conf.all.proxy_arp=1`
   and `net.ipv6.conf.all.proxy_ndp=1`.
3. The k3s Kubernetes version SHALL be within Substrate's supported range (latest stable or previous
   minor). WHEN the pinned nixpkgs `k3s` is older THEN the flake SHALL take `k3s` from a newer pinned
   nixpkgs input for that package only.
4. The k3s server SHALL enable the API features Substrate's certificate controller needs: feature gates
   `ClusterTrustBundle`, `ClusterTrustBundleProjection`, `PodCertificateRequest` on kube-apiserver (and on
   kubelet where required for projection), plus `runtime-config=certificates.k8s.io/v1beta1=true`.
5. Kubelet SHALL pull images in parallel (`serializeImagePulls: false`, `maxParallelImagePulls: 4`). The
   Substrate install pulls about 570 MB of third-party images.
6. The micro-VM sandbox class (Kata + Cloud Hypervisor) is **required on every node**. The NixOS config
   SHALL load `kvm-intel`/`kvm-amd` (vendor detected at install) and make `/dev/kvm` usable by containerd,
   so atelet advertises the device on every node. The platform phase SHALL always install the `microvm`
   `SandboxConfig`, alongside `gvisor`, and SHALL fail if any Ready node lacks `/dev/kvm`. AX tasks SHALL
   default to the `microvm` sandbox class; gVisor stays available. Substrate's
   `hack/install-microvm-deps.sh --install` SHALL run as part of the platform phase before the `microvm`
   SandboxConfig is applied.
7. A third deploy phase, `just platform`, SHALL install Substrate and then AX from **pinned upstream
   commits**, using their generic (non-kind) installers. Images are built with `ko` and pushed to
   `ghcr.io/<owner>/…` (Req 14). No in-cluster registry is used.
8. Pushing images SHALL use the operator's `gh` login (`gh auth token`, needs the `write:packages` scope:
   `gh auth refresh -s write:packages,read:packages`), falling back to `secrets/ghcr-push-token` (classic
   PAT, `write:packages`). The gh token never goes to nodes, because it also carries `repo` scope.
   Nodes use only the read-only `secrets/ghcr-token`. Substrate and AX images SHALL be pushed under
   `ghcr.io/<ghcr_owner>/` with `org.opencontainers.image.source` pointing at this repository, so they are
   grouped under it and stay private with it.
9. Substrate's stateful dependencies (PostgreSQL, rustfs S3 bucket for snapshots) SHALL run in-cluster on
   `local-path` volumes pinned to the server node, using the smallest cluster-size profile (`size0`).
10. The platform phase SHALL check that the required local tools exist (`go`, `kubectl`, `make`, `git`)
    and fail with install hints otherwise. `ko` SHALL be installed by the deploy itself at a pinned
    version into `.cache/bin` (`GOBIN=… go install github.com/google/ko@<ver>`).
11. WHEN the platform phase completes THEN the verify task SHALL check that `ate-system` and `ax-system`
    pods are Ready, run one AX sample task end-to-end in a micro-VM sandbox on **each** node and one in
    gVisor, and report the platform's RAM/CPU requests and actual usage per node against the budget.
12. The platform phase SHALL install only what AX needs: no demos, no benchmark/locust stack,
    `--setup-csi=none`, `--cluster-size size0`. Substrate's in-cluster telemetry SHALL be kept minimal.
    Once OpenObserve exists, control-plane telemetry SHALL go there via `--otlp-endpoint`, and any
    bundled Prometheus/Jaeger SHALL be removed (Req 12).

### Requirement 17 — Memory and CPU efficiency profile

**User Story:** As an operator, I want the whole stack (NixOS, k3s, Substrate/AX) tuned and measured for
memory and CPU efficiency, with clear customisation knobs, so that as much of each VM as possible goes to
my numpy/pandas, Java and agent workloads.

#### Acceptance Criteria

1. The base OS SHALL import NixOS's `profiles/minimal.nix` and disable remaining desktop-oriented services
   (polkit, udisks2, fontconfig, docs), unless a disabled service is needed by k3s or Substrate.
   `profiles/headless.nix` SHALL NOT be imported: it panics on boot failure and removes the console getty,
   which is the last-resort debugging path on a Proxmox VM.
2. Idle overhead targets per node, excluding workloads, SHALL be:
   - base OS ≤ 250 MiB
   - k3s ≤ 700 MiB on the server, ≤ 300 MiB on agents
   - kube-system add-ons ≤ 250 MiB in total
   - Substrate + AX: measured, with a target set after the first install
   - combined idle CPU of OS + k3s + add-ons ≤ 10% of one vCPU per node
3. `just footprint` SHALL report per node, grouped by layer (OS / k3s / kube-system / ate-system / ax-system
   / workloads): RSS, cgroup memory, CPU usage over a 60 s window, zram usage, and (when enabled) KSM
   sharing. Results SHALL be written to `reports/footprint-<timestamp>.md` (committed) so changes can be
   compared across runs.
4. Every efficiency/capability trade-off SHALL be a named knob in `group_data` with a documented default,
   effect, and when to change it. At least: swap mode/size/algorithm, pod swap behaviour, kubelet
   reservations and eviction, CPU manager policy, bundled k3s components, KSM, kernel preemption mode, CPU
   vulnerability mitigations, Substrate warm worker-pool sizes, and the Traefik replica count.
5. Kernel preemption SHALL be set for batch throughput (`preempt=voluntary`, configurable).
6. CPU vulnerability mitigations SHALL stay **enabled** by default, because micro-VM/gVisor sandboxes run
   untrusted agent code and rely on them. Disabling them is a documented knob with the security
   trade-off stated (it can help syscall-heavy workloads on this Coffee Lake CPU).
7. KSM (`hardware.ksm`) SHALL be available as a knob (default off) for micro-VM density. The verify task
   SHALL report `pages_sharing`/`pages_shared` when it is on, so it is only kept if it actually saves
   memory.
8. Substrate warm worker-pool sizes SHALL default to the minimum that keeps AX functional (one warm worker
   per sandbox class per node, or upstream minimum), and SHALL be configurable.
9. The workload guide (Req 15) SHALL additionally cover memory-efficiency runtime settings:
   - `MALLOC_ARENA_MAX=2` (or jemalloc) for multi-threaded Python and Java processes, to stop glibc arena
     bloat
   - JDK 25+ `-XX:+UseCompactObjectHeaders`, collector choice by heap size (Serial for small heaps,
     Parallel for throughput batch, G1/ZGC otherwise), and AppCDS for start-up/footprint
   - pandas Arrow-backed dtypes and Copy-on-Write, numpy `float32` where precision allows, and
     memory-mapped/chunked inputs
   - the matching `examples/` manifests SHALL set these by default
10. The Proxmox guide SHALL include vCPU/RAM sizing guidance for node VMs on the i7-8700 host (6 cores / 12
    threads): leave host headroom, and prefer adding vCPUs to the server before adding replicas.

### Requirement 18 — AX platform decisions (operator, 2026-10-04)

Research of Substrate v0.3.0 / AX v0.3.1 source showed: AX hard-codes the gVisor sandbox class and
sets no actor resources; AX has no fan-out (1 Task = 1 actor) and no auth/RBAC; Substrate picks workers
uniformly at random (no node awareness); actor images are pulled by atelet, which only has credentials
for Google registries; the generic install assumes GCP (GCS, Google image-pull auth); bundled Postgres
requests 2 CPU / 1 GiB with a 500 GiB PVC; Substrate telemetry is OTLP/gRPC only. Decisions:

1. AX runs **stock v0.3.1 on gVisor** (no AX patch). This supersedes Requirement 16.6 for AX tasks: the
   micro-VM SandboxConfig is deferred (KVM stays available on all nodes); `gvisor-default` is used.
2. **Task images are public** on `ghcr.io/norandom` (AX task-runner and workflow images). Substrate and
   AX control-plane images stay private and are pulled by kubelet with the node token.
3. Node VMs are resized to **4 vCPU / 8 GiB** each (operator does this in Proxmox; no reinstall).
4. Distribution: **one gVisor WorkerPool per node** (pinned via `nodeSelector` to kube-01 / kube-02,
   sharing a label used by `workerSelector`), so every node always has workers and Substrate's random
   placement spreads tasks across both. `just ax-fanout` submits N tasks for one workflow.
5. Install-level adaptations (no operator decision needed): S3 snapshots on the operator's
   **existing rustfs** (S3 API `http://10.0.0.54:9000`, bucket `k3s-ate-snapshots`, credentials in
   `secrets/rustfs-credentials` -> Secret `ate-s3-credentials`; no in-cluster rustfs), `--gcp-auth-for-image-pulls=false`, GKE-only PodMonitoring removed,
   Postgres trimmed (~250m CPU, small PVC), images built with ko + podman (no docker buildx),
   telemetry to the OpenObserve gateway's OTLP/gRPC port 4317, `AX_SNAPSHOTS_BUCKET` =
   `gs://k3s-ate-snapshots/ax/` (scheme ignored; atelet uses its S3 client). The deployer kubeconfig grants port-forward access to `ax-server`; AX itself has no
   per-user isolation, and its Redis state is not persisted.

### Requirement 19 — Actor egress (operator decisions, 2026-10-04)

1. Every actor in atespace `default` SHALL get internet access over **HTTPS (TCP 443) to any public
   host**, attached automatically by the operator-side `egress-watcher` (`tools/egress-watcher`, policy
   `tlsPassthrough` `hostnames: ["*"]`, `ports: [443]`) within ~1 s of actor start. The gateway's
   missing-policy deny cache SHALL be 2 s (`--egress-policy-cache-ttl=2s`). Task code retries its first
   connection for up to 60 s.
2. **Dropped for now (noted):** per-hostname scoping (originally: engines only to
   `files.because-security.com`). Reason: Substrate v0.3.0 ignores `tls_passthrough` rules; upstream
   implements them in #2019, but hostname enforcement also needs the listener rewiring of #2015.
   Restore by moving to a Substrate release containing both, then setting the watcher policy back to the
   hostname list. **Done 2026-10-06:** Substrate moved to main `715f2daa` (has #2019 + #2015);
   host names verified enforced (`scripts/egress-test.py`: test prefixes reach only their hosts).
3. Substrate ran **v0.3.0 + a backport of upstream #2019** until 2026-10-06. Without #2015 that
   backport let an actor that has *any* policy pass TCP to any destination and port, so a
   **NetworkPolicy on the egress gateway** (`platform/substrate/egress-netpol.yaml`) SHALL cap all actor
   egress at network level: public IPv4 on TCP 443 only; private/LAN/cluster ranges, other ports and
   plain HTTP blocked. Verified: AX Redis, ax-server, Kubernetes API, Traefik, LAN hosts unreachable from
   actors; public HTTPS reachable.
5. Actors named `ousast-engine-search-verify-*` SHALL get **no** egress policy (route with
   `"policy": null` in `platform/substrate/egress-routes/open.json`), i.e. loopback only (verified
   2026-10-05: pypi blocked, 127.0.0.1 reachable).
7. Per-prefix egress (agreed with OpenUltraSAST 2026-10-05; `egress-routes/scoped.json`), to be
   switched on only after the Substrate upgrade enforces host names (#2015), and only after telling
   OpenUltraSAST: `search-exec-` any public host; `search-verify-`, `search-probe-`, `pass-`
   files.because-security.com only; `replay-` files.because-security.com + github.com; all ports 443.
   The watcher picks the longest matching prefix; no match or `null` means no egress.
   **Live since 2026-10-06 08:15**; `scripts/egress-test.py scoped`: 40/40 as expected.
   Deny by default inside the namespace (OpenUltraSAST, 2026-10-06): any other `ousast-engine-*` name
   gets no egress (live 08:23; egress test 45/45).
6. Residual, accepted: actors can reach Substrate's API endpoint (the gateway must call it); it requires
   authentication actors do not have. AX itself has no authentication.

### Requirement 20 — Gitea Actions CI (operator decisions, 2026-10-04)

1. Gitea runners (gitea/runner v4.1.0) SHALL be registered with `https://code.because-security.com`:
   `kube-ax-pods` (labels `ubuntu-latest`, `kube-ax`; each job a Kubernetes pod) and `kube-ax-dind`
   (label `dind`; privileged Docker-in-Docker, for trusted repositories only).
2. **CI waits when AX is busy.** AX workers run one engine each (Substrate `--max-actors=1`), with a fixed
   3 GiB per worker at PriorityClass `ax-workers` (5000). CI runs at `batch-low` (1000) in the remaining
   capacity. Jobs beyond runner capacity (pods 2, DinD 1) queue in Gitea; job pods that don't fit stay
   Pending until room frees (3 h job timeout).
3. CI pods SHALL reach the internet but not the LAN or cluster-internal services (NetworkPolicy); the pods
   runner's RBAC is limited to its own namespace. The registration token lives in `secrets/`.
4. Consequence for AX: at most 2 concurrent engines (one per worker); a task submitted while both workers
   are busy fails in AX v0.3.1, so the dispatcher limits concurrency. Engine JVM `-XX:ActiveProcessorCount=2`.

### Requirement 21 — Disk and resource guardrails (after the 2026-10-05 outage)

**Context.** On 2026-10-05 (~17:50–18:10) Substrate's actor image cache (`/var/lib/ate/image-cache`,
18 versions of a task image that CI rebuilt from scratch on every merge, 14 GiB) filled the 20 GiB OS
volume. Kubelet evicted Substrate, AX and telemetry; `kube-state-metrics` was recreated ~3000 times;
both kubelets ran at ~300 % CPU on the dead-pod backlog; the telemetry gateway was OOM-killed.

1. Nothing that grows with workload SHALL live on the OS volume. `/var/lib/ate` SHALL be bind-mounted
   from the containerd volume (`ate_on_containerd`, default true; existing nodes migrated with
   `scripts/migrate-ate-dir.sh`).
2. The actor image cache SHALL be capped: atelet `--image-cache-max-bytes` 10 GiB (sized for
   OpenUltraSAST's rotation: engine ~3.6 GB + search ~2.1 GB unpacked, base layers shared), cleanup from
   75 % to 65 % of the volume. Order on the containerd volume: cache cap → atelet 75 % → kubelet image
   GC 80 % → kubelet eviction 85 %.
3. Kubernetes SHALL keep at most 100 terminated pods (`terminated-pod-gc-threshold=100`).
4. Clean page cache left by deleted pods SHALL be dropped nightly (03:30) so dashboards show real use.
5. `just verify` SHALL fail while there is still room: `/` or the containerd volume < 25 % free, cache
   > 11 GiB, `/var/lib/ate` not on the containerd volume.
6. Every new tenant that grows (cache, log, queue, scratch) SHALL answer the checklist in
   `docs/guardrails.md` (where it writes, its cap, who cleans it and when, limits, a verify check, what
   it leaves behind).
7. Alerts (2026-10-09): OpenObserve to Discord for disk free < 25 % and < 15 % per volume and for node
   pressure / eviction storms; single evictions do not alert.
8. Known gaps (accepted for now): no alert on missing events or restart rate, no per-namespace
   quotas, several platform pods without memory limits, no per-pod PID limit.

### Requirement 22 — Substrate version and upgrades (2026-10-06)

1. Substrate SHALL run upstream main `715f2daa` (`v0.3.0-34-g715f2daa`): it contains #2019 (TLS
   passthrough) and #2015 (host names enforced) and stops before #2105, which removes
   `snapshotConfig.onResume` that AX v0.3.1 still sends (AX would break). A later bump needs an AX that
   no longer sends that field.
2. Our changes SHALL be anchored edits (`platform/substrate/kube-ax-edits.py`): fail loudly when an
   anchor disappears, no-op when re-run. The v0.3.0 diff (`kube-ax.patch`) stays for rollback only.
3. Upgrades SHALL use `scripts/substrate-upgrade.sh OLD_VERSION` (stop-the-world, adapted from
   upstream `docs/upgrade.md`): refuse while any worker hosts an actor, back up Postgres, deploy
   controller and atelet, delete worker pools, relabel nodes, deploy API server and the rest, recreate
   pools, remove the old atelet, restart every client of the Substrate API.
4. Clients of the headless `api` service (AX controller/server, egress-watcher, reaper) keep the old API
   server pod's IP after it is replaced and SHALL be restarted. Same symptom outside upgrades ("dial tcp
   …:443: i/o timeout" in ax-controller): restart AX.
5. After an upgrade, `scripts/egress-test.py scoped` and `just verify` SHALL pass before users are told.

### Requirement 23 — AX task lifecycle for OpenUltraSAST (2026-10-05/06)

1. **Reaper** (`tools/ax-reaper`): tasks named `ousast-engine-*` SHALL be deleted 10 min after their
   command exited (runner's exit line in the worker log, newer than the task's creation). Running tasks
   and other prefixes are never touched. Deletes through AX; read-only Kubernetes access.
2. **Golden snapshot copy.** For every new task (= new ActorTemplate) Substrate boots one short copy in
   atespace `ate-golden`, snapshots it and deletes it (~1 s on a worker). The copy starts the task command
   but has no egress. It can make a submission in that second fail with "no free workers". Clients SHALL
   treat `ResourceExhausted "no free workers available"` as retry (2–5 s, up to ~1 min) and count runs only
   on `ate.atespace == default`.
3. At most 2 tasks in flight (one per worker). Tasks SHALL be deleted as soon as their result is read.
4. Task images: shared base, code in the last layer, rebuilt only on demand; all images in rotation
   together ≤ ~9 GiB unpacked (measured 2026-10-06: ~4.5 GB).

### Requirement 24 — Where a task's files live (measured 2026-10-06)

1. Measured with 1 GB written to each location inside a task (worker cgroup, node disk):

   | Path | Backed by | Counts against the 3 GiB worker as |
   |---|---|---|
   | `/` (image root) | file on node disk (gVisor overlay `root:self`) | page cache, reclaimable |
   | `/workspace` | node disk (`/var/lib/ate`, containerd volume) | page cache, reclaimable |
   | `/tmp` | RAM disk inside the sandbox (tmpfs) | shared memory, **not** reclaimable |

2. Task authors SHALL keep large temporary files out of `/tmp` (`TMPDIR=/workspace/tmp` or `/var/tmp`).
   Earlier guidance ("`/` and `/tmp` are RAM") was wrong for `/` and is corrected.
3. The planned "disk-backed sandbox test pool" (runsc `--overlay2=root:self`) is **dropped**: gVisor
   already runs with `root:self`. Moving `/tmp` to disk would need a Substrate change (a `/tmp` mount in the
   actor's OCI spec); not pursued while `TMPDIR` solves it.
