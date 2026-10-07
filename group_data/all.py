# Cluster-wide defaults. Any key can be overridden per host in inventory.py.
# Knobs and when to change them: .kiro/specs/k3s-nixos-cluster/design.md, "Customisation knobs".
from pathlib import Path

cluster_name = "kube-ax"

# SSH: installed nodes are reached as the admin user; install runs with --data ssh_user=nixos.
ssh_user = "ops"
admin_user = "ops"
admin_ssh_keys = [Path("~/.ssh/id_ed25519.pub").expanduser().read_text().strip()]

# disk: GPT -> LVM VG "pool". Sizes per logical volume; the rest of the disk stays free so
# any LV can grow online (lvextend -r). containerd gets its own filesystem so kubelet tracks
# imagefs separately.
disk_layout = {"root": "20G", "containerd": "35G", "storage": "30G"}

# VM size set in Proxmox; verify fails if a guest sees less (e.g. balloon inflated).
node_memory_gib = 8

# k3s
k3s_disable = []  # keep traefik, servicelb, metrics-server, local-storage, coredns
k3s_flannel_backend = "auto"  # host-gw when all nodes share the server's subnet, else vxlan
traefik_replicas = 1
# /var/lib/ate (actor image cache + task /workspace) on the containerd volume instead of the 20 GiB
# OS volume. New nodes get it at install; existing nodes switch via scripts/migrate-ate-dir.sh,
# which moves the data first and then sets this per host in inventory.py.
ate_on_containerd = True

# kubelet
system_reserved = {"cpu": "200m", "memory": "256Mi"}
kube_reserved = {"cpu": "200m", "memory": "256Mi"}
eviction_hard = {"memory.available": "256Mi", "nodefs.available": "10%", "imagefs.available": "15%"}
eviction_soft = {"memory.available": "512Mi"}
eviction_soft_grace = {"memory.available": "1m30s"}
swap_behavior = "NoSwap"  # pods never swap; "LimitedSwap" opt-in per host
cpu_manager_policy = "none"  # "static" needs reserved_cpus, e.g. "0"
reserved_cpus = None
image_gc_high = 80
image_gc_low = 60

# host tuning
swap_mode = "zram"  # "zram" | "zswap" | "none"
zram_percent = 25
zram_algorithm = "lz4"
zswap_swapfile_gb = 4
ksm = False
kernel_preempt = "voluntary"
cpu_mitigations = "auto"  # "off" only if no untrusted agent code runs
cpuidle_haltpoll = False  # idle vCPUs poll before halting: lower latency, more host CPU
soft_lockup_watchdog = False  # off: no false alarms when the Proxmox host is busy
nofile_soft = 65536  # systemd DefaultLimitNOFILE (Java, agents)
nofile_hard = 524288
sysctl_overrides = {}

# registry (Requirement 14)
ghcr_owner = "norandom"  # images: ghcr.io/norandom/<image>, linked to this repo
