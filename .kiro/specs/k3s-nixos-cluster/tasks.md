# Implementation Plan

- [x] 1. Scaffold the operator project
  - [x] 1.1 `git init`, `.gitignore` (`.venv/`, `secrets/`, `__pycache__/`, `result*`), `.pre-commit-config.yaml` with a hook that rejects paths under `secrets/`
    - _Requirements: 1.5, 9.7_
  - [x] 1.2 `.python-version`, `pyproject.toml` (pyinfra 3.x, `rust-just`; dev: pytest, ruff), `uv lock`, `uv sync` → `./.venv`
    - _Requirements: 1.1, 1.2_
  - [x] 1.3 `justfile`: every recipe calls `uv run …`; recipes `setup`, `install`, `k3s`, `platform`, `verify`, `kubeconfig`, `kubectl`, `add-node`, `test`, `nix-check` (bodies may be stubs)
    - _Requirements: 1.2, 1.3_

- [x] 2. Inventory, group data, node config rendering
  - [x] 2.1 `inventory.py`: kube-01 (10.0.0.63, `/dev/sda`, bios) under `servers`; `agents` commented placeholder until the operator supplies the IP
    - _Requirements: 2.1, 2.2_
  - [x] 2.2 `group_data/all.py`, `servers.py`, `agents.py` with design defaults (admin `ops`, keys, k3s, kubelet, swap/tuning, ghcr, cluster name, `kubeconfig_encryption`)
    - _Requirements: 2.1, 7.1, 7.4, 7.5, 13.13_
  - [x] 2.3 `lib/nodeconfig.py`: validation, merge of `host_network/<node>.json` + inventory overrides, flannel auto-selection, server/agent shaping, kubelet + tuning sections, `schema: 1`
    - _Requirements: 2.3, 2.4, 4.3, 7.3, 8.3, 13.13_
  - [x] 2.4 pytest for nodeconfig (missing field, single server, flannel, network override precedence, swap modes, kubelet rendering)
    - _Requirements: 2.2, 2.3, 4.3, 7.3, 13.2_

- [ ] 3. Nix flake and modules
  - [x] 3.1 `nix/flake.nix`: inputs nixpkgs `nixos-26.05` + disko; outputs `nixosConfigurations.node` (reads `./node.json`) and `fixture-*` configs; `flake.lock` (nixpkgs 26.05.20261002). Installer ISO output deferred with 3.6
    - _Requirements: 1.4, 3.10_
  - [x] 3.2 `nix/disko.nix`: BIOS (EF02, GRUB) and UEFI (ESP, systemd-boot) + LVM VG `pool` with ext4 `noatime` LVs root/containerd/storage
    - _Requirements: 3.4, 13.12_
  - [x] 3.3 `modules/base.nix`: minimal profile (headless.nix deliberately not imported, see Req 17.1), hostname, networkd static config matched by MAC (no DHCP), `ops` user + keys, passwordless sudo, hardened sshd, qemu guest agent, no docs, journald caps, nix gc/optimise, stateVersion 26.05
    - _Requirements: 3.6, 4.2, 4.5, 4.6, 4.7_
  - [x] 3.4 `modules/tuning.nix`: `preempt=voluntary`, mitigations knob, KSM knob, `swapMode` zram(lz4, 25%)/zswap/none, sysctls (incl. Substrate `proxy_arp`/`proxy_ndp`), THP madvise + defrag, MGLRU `min_ttl_ms`, `systemd.oomd` off, no earlyoom, fstrim, `kvm-<vendor>` module when `node.kvm` is set; all `mkDefault` with `node.tuning.sysctl` overrides
    - _Requirements: 13.1, 13.3–13.7, 13.9, 13.12, 13.13, 16.2, 16.6_
  - [x] 3.5 `modules/k3s.nix` (gated on `phase == "k3s"`):
    - `services.k3s` role/token/serverAddr/flags
    - KubeletConfiguration file (failSwapOn false, swapBehavior, reservations, enforceNodeAllocatable, eviction soft/hard, kernelMemcgNotification, imageGC, cpuManager)
    - k3s unit OOMScoreAdjust/CPUWeight/IOWeight
    - server-only: servicelb label, Traefik HelmChartConfig (1 replica, pinned), PriorityClasses, firewall per role
    - Substrate support: apiserver feature gates + `runtime-config`, kubelet `serializeImagePulls: false` / `maxParallelImagePulls: 4`
    - **Verified at eval time**: option names/types (`extraFlags` list, `manifests`, `nodeIP`, `nodeLabel`, `disable`, `package`); k3s pinned to `pkgs.k3s_1_36` (1.36.4; the 26.05 default is 1.35.8, below Substrate's range) from the same nixpkgs, so no extra input. Kubelet settings ship as drop-in `kubelet.conf.d/99-kube-ax.conf` so k3s's `00-k3s-defaults.conf` can't override them.
    - **Still to verify on the live node (task 7.6)**: effective kubelet config via `/configz` (drop-in applied, NoSwap with `failSwapOn: false`), Substrate feature gates accepted by 1.36, servicelb ports reachable through the NixOS firewall
    - _Requirements: 3.8, 5.1–5.4, 6.1, 7.1–7.5, 8.1, 8.3, 11.1, 11.2, 11.5, 12.1, 12.2, 13.2, 13.8, 13.10, 13.11, 15.2, 16.3, 16.4, 16.5_
  - [ ] 3.6 *(optional, deferred: stock ISO is the default)* `modules/installer.nix`: ISO with `nixos` authorized keys, sshd on, qemu guest agent on
    - _Requirements: 3.9, 3.10_
  - [x] 3.7 Fixture `node.json` files (server/bios/zram, agent/uefi/zswap, static CPU manager) + `just nix-check` evaluating each `toplevel.drvPath` inside the pinned `nixos/nix` podman container (no local Nix)
    - _Requirements: 1.2a, 1.4, 2.4_

- [x] 4. Custom pyinfra facts (`lib/facts.py`)
  - `IsLiveIso`, `BootMode`, `DiskState`, `PrimaryNetwork`, `CpuCapabilities`, `K3sNodeReady`, `TuningState`. Validated against 10.0.0.63 with `just probe 10.0.0.63` (deploys/probe.py) on 2026-10-04: live ISO, BIOS, blank 100 GiB disk, DHCP 10.0.0.63/24 gw/dns 10.0.0.1, i7-8700 AVX2/FMA/vmx + `/dev/kvm`. `K3sNodeReady`/`TuningState` k3s parts get exercised in task 7. Note: `systemctl show` reports `OOMScoreAdjust=0` for a missing unit, so verify must check that the unit exists first
  - _Requirements: 3.1–3.3, 4.1, 5.5, 6.3, 13.16, 16.6_

- [ ] 5. ISO hand-off
  - [x] 5.1 ~~`just bootstrap-iso`~~ dropped (operator decision): manual `passwd` + `ssh-copy-id nixos@<ip>` instead, documented in `docs/proxmox.md` (5.3) and in install's connection-failure hint (6.1)
    - _Requirements: 3.10_
  - [ ] 5.2 *(optional, defer)* `just iso`: custom installer ISO built in podman or on kube-01
    - _Requirements: 3.11_
  - [ ] 5.3 `docs/proxmox.md`: VM settings (boot order disk→CD, CPU `host`, ballooning off, VirtIO SCSI + discard + IO thread, virtio NIC), DHCP reservation advice
    - _Requirements: 3.12, 4.4, 13.14_

- [x] 6. Install deploy (`deploys/install.py`)
  - [x] 6.1 Preconditions: skip non-ISO hosts; fail on missing AVX2/FMA/vmx-svm or `/dev/kvm`; fail on DHCP address ≠ inventory, on boot-mode mismatch, and on a non-empty disk without `wipe_disk=<disk>`
    - _Requirements: 3.1–3.3, 3.13, 4.1, 10.2_
  - [x] 6.2 Capture `PrimaryNetwork` → `host_network/<node>.json` (refuse silent changes unless `recapture_network=True`)
    - _Requirements: 4.1, 4.3_
  - [x] 6.3 Upload flake + `node.json` (`phase=base`); disko `destroy,format,mount`; `nixos-generate-config --no-filesystems --root /mnt`; copy to `/mnt/etc/nixos`; `nixos-install --flake path:/mnt/etc/nixos#node --no-root-passwd`
    - _Requirements: 3.4, 3.5, 3.6, 3.8_
  - [x] 6.4 Delayed reboot, `ssh-keygen -R`, wait for `ops@<ip>`
    - _Requirements: 3.7, 4.2_
  - [x] 6.5 Ran on 10.0.0.63 on 2026-10-04 (blank disk, so no WIPE needed). Verified: static 10.0.0.63/24 via MAC-matched networkd, LVM root/containerd/storage `noatime` with 15 GiB free, zram lz4 1.4G, kernel cmdline/sysctls/THP/MGLRU as designed, `/dev/kvm`, no failed units. Idle base OS ≈ 115 MiB non-reclaimable (anon 28 + slab 51 + kernel ~34); `free`'s "used" (497 MiB) also counts the watermark reserve, so footprint reporting must use `/proc/meminfo` components
    - _Requirements: 3.1–3.8, 4.1–4.8, 13.1_

- [x] 7. k3s configure deploy (`deploys/k3s.py`)
  - [x] 7.1 `lib/secrets.py`: generate `secrets/k3s-token`; upload to `/var/lib/k3s-secrets/token` (root 0600)
    - _Requirements: 9.1, 9.2_
  - [x] 7.2 Sync flake (keep `hardware-configuration.nix`), upload `node.json` (`phase=k3s`), `nixos-rebuild switch` only on change
    - _Requirements: 5.1, 6.1, 10.1_
  - [x] 7.3 ghcr: write `/etc/rancher/k3s/registries.yaml` (0600) from `secrets/ghcr-token` when present; restart k3s only on change; warn when absent
    - _Requirements: 14.1–14.4_
  - [x] 7.4 Ready waits (server self; agents checked from the server, 5 min timeout, journal dump on failure)
    - _Requirements: 5.5, 6.2, 6.3_
  - [x] 7.5 `just k3s` ordering: servers, then agents `--serial`; optional single host
    - _Requirements: 6.2, 10.3, 10.4_
  - [x] 7.6 Apply to kube-01. Confirm SQLite datastore, no taint, Traefik on kube-01, LB only on server IP, k3s RSS within budget
    - Done 2026-10-04: kube-01 Ready on k3s 1.36.4+k3s1 / containerd 2.3.4, SQLite datastore (no etcd), no taints, Traefik pinned to kube-01 with LB IP 10.0.0.63 only, ingress smoke test HTTP 200 via 10.0.0.63. `/configz` confirms every value from `99-kube-ax.conf` wins over `00-k3s-defaults.conf`/`10-cli-config.conf` (NoSwap + failSwapOn false, reservations, eviction, memcg notification, image GC, parallel pulls, Substrate feature gates). Substrate APIs served (`certificates.k8s.io/v1beta1`: clustertrustbundles, podcertificaterequests). Re-run: 0 changes, k3s not restarted
    - Over budget right after start: k3s-server 674 MiB + containerd 190 MiB = 864 MiB (target 700); add-ons 258 MiB (target 250). Re-measure after settling in 10.7; `GOMEMLIMIT` on k3s is the main lever
    - _Requirements: 5.2, 5.3, 7.6, 11.1, 11.2_

- [x] 8. Kubeconfig (`deploys/kubeconfig.py`)
  - [x] 8.1 Fetch `k3s.yaml`, rewrite server URL + rename `default` → cluster name, write `kubeconfig/<cluster>.yaml` (0600); called at end of `just k3s`
    - _Requirements: 9.3_
  - [x] 8.2 `just kubectl` wrapper using `kubeconfig/<cluster>.yaml` (plaintext; sops support deferred)
    - _Requirements: 9.5_
  - [x] 8.3 Local commit with pathspec only when content changed; skip + warn if `origin` is a public GitHub repo; never push
    - _Requirements: 9.4, 9.6_

- [x] 9. Verify deploy (`deploys/verify.py`)
  - Nodes Ready; system pods Running; spread-test Deployment across all nodes; Ingress via server IP to a pod pinned on an agent (HTTP 200); LB port closed on agents
  - Per node: zram/swap mode, key sysctls, k3s `OOMScoreAdjust=-999`, `free -m`, k3s RSS, image FS usage; ghcr test-image pull when configured
  - _Requirements: 4.8, 7.6, 8.4, 11.3, 11.4, 13.16, 14.5, 14.6_

- [ ] 10. Platform phase: Agent Substrate + AX (`deploys/platform.py`)
  - [ ] 10.1 `platform/versions.toml` with pinned substrate + ax commits and tool minimums; `just platform-bump` to update them deliberately
    - _Requirements: 16.7_
  - [ ] 10.2 Preflight: local tools, pinned `ko` into `.cache/bin`, resolve `ghcr_owner`, k8s version range, ClusterTrustBundle API present, `ko login ghcr.io` from `secrets/ghcr-push-token`
    - _Requirements: 16.3, 16.4, 16.8, 16.10_
  - [ ] 10.3 Read the pinned `hack/install-ate.sh` and find the generic (non-kind/GCP) path: required env, CSI choice, PostgreSQL/rustfs options. Record overrides in `platform/values/`
    - _Requirements: 16.7, 16.9_
  - [ ] 10.4 Install Substrate (`size0`, `KO_DOCKER_REPO=ghcr.io/<owner>/substrate`); pin PostgreSQL + rustfs to the server; apply `microvm` (default class) + `gvisor` SandboxConfigs; fail if any node lacks `/dev/kvm`
    - _Requirements: 16.1, 16.6, 16.7, 16.9, 16.12_
  - [ ] 10.5 Install AX (`make deploy AX_IMAGE_REPO=ghcr.io/<owner>/ax`) and the `ax` CLI at the pinned commit
    - _Requirements: 16.7_
  - [ ] 10.6 Extend verify: `ate-system`/`ax-system` Ready, AX sample task in a micro-VM on each node + one in gVisor, platform RAM/CPU per node vs. budget; then update the design's resource budget with the measured numbers
    - _Requirements: 16.11_
  - [ ] 10.7 Efficiency pass (Req 17)
    - `just footprint` (`deploys/footprint.py`): per-layer RSS/cgroup memory, 60 s CPU, zram, KSM → `reports/footprint-<ts>.md`
    - Baseline report after k3s, again after platform; set the Substrate/AX target in the design budget; tune knobs (reservations, warm workers, KSM trial) and keep only changes that improve the report
    - _Requirements: 17.2, 17.3, 17.7, 17.8_

- [ ] 11. Workload guide and examples
  - `docs/workloads.md`:
    - BLAS/OMP thread env vars and pandas memory tips
    - JVM container flags (`MaxRAMPercentage`, `ActiveProcessorCount`, G1/ZGC, THP)
    - requests/limits policy (memory req = limit; CPU no limit for batch), QoS
    - PriorityClasses
    - layered OOM model and why there's no earlyoom
  - Memory-efficiency runtime settings: `MALLOC_ARENA_MAX`/jemalloc, JDK 25 compact object headers + GC choice + AppCDS, pandas Arrow dtypes + CoW, numpy float32, chunked/mmap inputs
  - `examples/`: pandas Job, numpy Job, Java Deployment (all with these defaults), memory-pressure smoke-test Job (`stress-ng`, `batch-low`)
  - `docs/proxmox.md`: vCPU/RAM sizing on the i7-8700 host
  - _Requirements: 13.15, 15.1, 15.3, 17.9, 17.10_

- [x] 12. Add the second node end-to-end
  - Create VM in Proxmox per `docs/proxmox.md`, boot installer ISO, add inventory line with the operator-supplied IP
  - `just add-node <ip> WIPE=/dev/sda`. Confirm kube-01 reports 0 changes and verify passes on both nodes
  - _Requirements: 3.9, 6.1–6.3, 8.4, 10.3_
  - Done 2026-10-04: kube-02 = 10.0.0.58 (110 GiB disk, BIOS, i7-8700 AVX2/FMA, KVM). install 8/8, k3s join, `just verify` 32/32 incl. spread across both nodes and ingress via 10.0.0.63 to a pod on kube-02
  - Bug found and fixed: k8s 1.36 enforces feature-gate dependencies; ClusterTrustBundleProjection needs ClusterTrustBundle. The server inherited it from its apiserver flag (one k3s process); the agent kubelet panicked. Now set explicitly in the kubelet drop-in on every node

- [x] 12a. `just kube-local` (scripts/kube-local.sh): merge kubeconfig into ~/.kube/config for kubectl/k9s/ktop/helm; replaces stale cluster entries, keeps other contexts, backs up, 0600

- [ ] 13. Idempotency, stress check, README
  - Re-run `just k3s`: 0 changes, unchanged k3s `ActiveEnterTimestamp`, kubeconfig file unchanged
  - Run the memory-pressure smoke test. Pods get evicted (not globally OOM-killed); node, k3s and SSH stay responsive
  - `README.md`: quickstart (`just setup` → `just iso` → Proxmox → `just add-node` → `just platform`), upgrades (`nix flake update` → `just k3s`), node reset, pushing to the private GitHub repo
  - _Requirements: 9.4, 10.1, 10.3, 10.4, 13.10_

- [x] 14. Disk-pressure guardrails (Req 21), after the 2026-10-05 outage
  - [x] 14.1 `/var/lib/ate` bind mount from the containerd LV (`ate_on_containerd`), both nodes migrated (image cache kept)
  - [x] 14.2 atelet image cache cap 10 GiB, watermarks 75/65 % (first pass freed 8.5 GB per node)
  - [x] 14.3 `terminated-pod-gc-threshold=100`; nightly clean page-cache drop (03:30)
  - [x] 14.4 verify: disk free ≥ 25 %, cache ≤ 11 GiB, `/var/lib/ate` mount source (46 checks)
  - [x] 14.5 `docs/guardrails.md` (rules, outage timeline, per-resource limits, checklist, gaps)
  - [ ] 14.6 OpenObserve alerts: disk free < 25 % per volume, pod restart rate
  - [ ] 14.7 Memory limits for Substrate, cert-manager, Traefik, metrics-server, OTel operator pods

- [x] 15. Substrate upgrade to main `715f2daa` (Req 22)
  - [x] 15.1 `kube-ax-edits.py` (anchored, idempotent); `platform.sh` pinned to the new commit
  - [x] 15.2 `scripts/substrate-upgrade.sh` (stop-the-world); ran 2026-10-06 08:03–08:06, no schema migration
  - [x] 15.3 restart AX/egress-watcher/reaper after the API server moves (headless service keeps old IP)
  - [x] 15.4 `scripts/egress-test.py`; baseline 20/20 on v0.3.0, 30/30 after, scoped 45/45

- [x] 16. Per-prefix egress for OpenUltraSAST (Req 19.7, 23)
  - [x] 16.1 egress-watcher routes (longest prefix, `null` = no egress), strict parsing, tests on the committed files
  - [x] 16.2 `scoped.json` live 2026-10-06 08:15; deny-by-default for unlisted `ousast-engine-*` at 08:23

- [x] 17. AX task lifecycle (Req 23)
  - [x] 17.1 `tools/ax-reaper`: delete `ousast-engine-*` 10 min after exit (tested: deleted after 10 min 42 s)
  - [x] 17.2 Golden-snapshot copy explained to OpenUltraSAST; retry on "no free workers"

- [x] 18. Where task files live (Req 24)
  - [x] 18.1 Measured `/`, `/tmp`, `/workspace` (1 GB each): only `/tmp` is RAM
  - [x] 18.2 Disk-backed sandbox test pool dropped (gVisor already `root:self`); guidance: `TMPDIR` on disk
