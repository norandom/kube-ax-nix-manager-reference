# Host tuning for CPU/memory-heavy jobs and micro-VM sandboxes (Req 13, 16.2, 16.6, 17).
# Every value is a default; per-host overrides come from node.tuning (group_data/inventory).
{ lib, node, ... }:
let
  t = node.tuning;

  sysctlDefaults = {
    # Compressed swap holds only OS/k3s/idle pages; pods are NoSwap (kubelet config).
    "vm.swappiness" = 100;
    "vm.page-cluster" = 0;
    "vm.watermark_boost_factor" = 0;
    "vm.watermark_scale_factor" = 125;
    "vm.min_free_kbytes" = 65536;
    "vm.dirty_background_bytes" = 67108864;
    "vm.dirty_bytes" = 268435456;
    "vm.max_map_count" = 262144;
    # dentry/inode cache is costly to rebuild and can't spill to zram: keep it longer.
    "vm.vfs_cache_pressure" = 50;
    # With dirty bursts bounded above, the flusher can wake every 15 s instead of 5 s.
    "vm.dirty_writeback_centisecs" = 1500;
    # What kubelet expects.
    "vm.overcommit_memory" = 1;
    "vm.panic_on_oom" = 0;
    "kernel.panic" = 10;
    "kernel.panic_on_oops" = 1;
    "kernel.pid_max" = 4194304;
    # Soft-lockup watchdog raises false alarms when the Proxmox host is busy.
    "kernel.watchdog" = if t.softLockupWatchdog then 1 else 0;
    # Container density. NixOS 26.05 already defaults max_user_instances/watches to 524288;
    # these only ever raise values, never lower a distro default.
    "fs.inotify.max_user_instances" = 524288;
    "fs.inotify.max_user_watches" = 524288;
    "fs.file-max" = 2097152;
    "net.netfilter.nf_conntrack_max" = 262144;
    # Networking.
    "net.ipv4.ip_forward" = 1;
    "net.bridge.bridge-nf-call-iptables" = 1;
    "net.bridge.bridge-nf-call-ip6tables" = 1;
    "net.core.default_qdisc" = "fq";
    "net.ipv4.tcp_congestion_control" = "bbr";
    "net.core.somaxconn" = 4096;
    "net.core.netdev_max_backlog" = 16384;
    "net.core.netdev_budget" = 600;
    "net.ipv4.tcp_fastopen" = 3;
    # Autotuned ceilings: memory is only used by connections that need large windows.
    "net.core.rmem_max" = 134217728;
    "net.core.wmem_max" = 134217728;
    "net.ipv4.tcp_rmem" = "4096 87380 134217728";
    "net.ipv4.tcp_wmem" = "4096 65536 134217728";
    "net.ipv4.tcp_no_metrics_save" = 1;
    "net.ipv4.tcp_mtu_probing" = 1;
    "net.ipv4.tcp_max_syn_backlog" = 8192;
    "net.ipv4.ip_local_port_range" = "1024 65000";
    # Agent Substrate / gVisor pod networking.
    "net.ipv4.conf.all.proxy_arp" = 1;
    "net.ipv6.conf.all.proxy_ndp" = 1;
  };
in
{
  boot.kernelModules = [
    "br_netfilter"
    "overlay"
    "tcp_bbr"
    "nf_conntrack"
    # Micro-VM sandboxes (Kata + Cloud Hypervisor) need /dev/kvm on every node.
    "kvm-${node.kvm}"
  ];

  boot.kernelParams = [
    "transparent_hugepage=madvise"
    "preempt=${t.kernelPreempt}"
    # k3s doesn't use kernel audit; dropping it makes syscalls cheaper.
    "audit=0"
  ]
  ++ lib.optional (t.cpuMitigations == "off") "mitigations=off"
  ++ lib.optionals t.cpuidleHaltpoll [
    "cpuidle_haltpoll.force=1"
    "cpuidle.governor=haltpoll"
  ]
  ++ lib.optionals (t.swapMode == "zswap") [
    "zswap.enabled=1"
    "zswap.compressor=lz4"
    "zswap.max_pool_percent=20"
    "zswap.shrinker_enabled=1"
  ];

  # Priority 900: above distro mkDefaults (1000, which define some of the same keys) but below
  # plain definitions, so per-host node.tuning.sysctl overrides always win.
  boot.kernel.sysctl = lib.mkMerge [
    (lib.mapAttrs (_: lib.mkOverride 900) sysctlDefaults)
    t.sysctl
  ];

  zramSwap = lib.mkIf (t.swapMode == "zram") {
    enable = true;
    algorithm = t.zramAlgorithm;
    memoryPercent = t.zramPercent;
    priority = 100;
  };
  swapDevices = lib.optional (t.swapMode == "zswap") {
    device = "/var/lib/swapfile";
    size = t.zswapSwapfileGb * 1024;
  };

  systemd.tmpfiles.rules = [
    "w /sys/kernel/mm/transparent_hugepage/defrag - - - - defer+madvise"
    # MGLRU thrash protection: reclaim gives up (-> OOM/eviction) instead of thrashing.
    "w /sys/kernel/mm/lru_gen/enabled - - - - y"
    "w /sys/kernel/mm/lru_gen/min_ttl_ms - - - - 1000"
  ];

  hardware.ksm = {
    enable = t.ksm;
    sleep = 200;
  };

  # Kubelet eviction is the node's earlyoom equivalent; no second userspace killer (Req 13.9).
  systemd.oomd.enable = false;

  services.fstrim.enable = true;

  # The Proxmox host schedules the real disks; don't schedule twice in the guest.
  services.udev.extraRules = ''
    ACTION=="add|change", KERNEL=="sd[a-z]|vd[a-z]", ATTR{queue/scheduler}="none"
  '';

  boot.blacklistedKernelModules = [
    "floppy"
    "pcspkr"
    "joydev"
  ];

  systemd.settings.Manager.DefaultLimitNOFILE = "${toString t.nofile.soft}:${toString t.nofile.hard}";

  # /tmp stays on disk (no RAM cost) but starts empty after every boot.
  boot.tmp.cleanOnBoot = true;

  # A crashing JVM or numpy job must not fill the root LV with core dumps.
  systemd.coredump.settings.Coredump = {
    MaxUse = "500M";
    KeepFree = "1G";
  };

  # Nightly: drop clean page cache. Deleted pods leave image/binary cache billed to their (dying)
  # cgroups: measured 2.4-2.8 GiB per node on 2026-10-05, which made `kubectl top` show 20-27 %.
  # The kernel would free it under pressure anyway; this keeps dashboards showing real usage.
  # Only clean cache is dropped (no dirty data lost); the next reads come from disk once.
  systemd.services.drop-clean-cache = {
    description = "Drop clean page cache left by deleted pods";
    serviceConfig.Type = "oneshot";
    script = "sync; echo 1 > /proc/sys/vm/drop_caches";
  };
  systemd.timers.drop-clean-cache = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "*-*-* 03:30";
      RandomizedDelaySec = "10m";
      Persistent = true;
    };
  };
}
