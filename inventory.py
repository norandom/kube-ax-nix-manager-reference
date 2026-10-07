# Cluster inventory: the single source of truth for nodes (Requirement 2).
#
# Required per host: node_name, disk, boot_mode ("bios" | "uefi").
# The address is the IP the VM got from DHCP while booted from the NixOS ISO;
# install captures that lease and pins it statically (host_network/<node>.json).
# Optional per-host overrides: ip_prefix, gateway, dns, plus any key from group_data/all.py.
#
# Exactly one server. It is the control plane, a workload node, and the single entry IP.

servers = [
    (
        "10.0.0.63",
        {
            "node_name": "kube-01",
            "disk": "/dev/sda",
            "boot_mode": "bios",
            "ate_on_containerd": True,
        },
    ),
]

agents = [
    (
        "10.0.0.58",
        {
            "node_name": "kube-02",
            "disk": "/dev/sda",
            "boot_mode": "bios",
            "ate_on_containerd": True,
        },
    ),
]
