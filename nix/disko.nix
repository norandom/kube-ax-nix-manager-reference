# GPT -> (BIOS boot | ESP) + LVM PV -> VG "pool" -> LVs root / containerd / storage (Req 3.4).
# Unallocated extents stay free so any LV can grow online with `lvextend -r`.
{ node, ... }:
let
  bios = node.bootMode == "bios";
  ext4 = mountpoint: {
    type = "filesystem";
    format = "ext4";
    inherit mountpoint;
    mountOptions = [ "noatime" ];
  };
in
{
  disko.devices = {
    disk.main = {
      type = "disk";
      device = node.disk;
      content = {
        type = "gpt";
        partitions =
          (
            if bios then
              {
                # GRUB core image; disko adds this disk to boot.loader.grub.devices.
                boot = {
                  size = "1M";
                  type = "EF02";
                  priority = 1;
                };
              }
            else
              {
                ESP = {
                  size = "512M";
                  type = "EF00";
                  priority = 1;
                  content = {
                    type = "filesystem";
                    format = "vfat";
                    mountpoint = "/boot";
                    mountOptions = [ "umask=0077" ];
                  };
                };
              }
          )
          // {
            pv = {
              size = "100%";
              content = {
                type = "lvm_pv";
                vg = "pool";
              };
            };
          };
      };
    };

    lvm_vg.pool = {
      type = "lvm_vg";
      lvs = {
        root = {
          size = node.diskLayout.root;
          content = ext4 "/";
        };
        # Separate filesystem = separate kubelet imagefs (own eviction threshold and image GC).
        containerd = {
          size = node.diskLayout.containerd;
          content = ext4 "/var/lib/rancher/k3s/agent/containerd";
        };
        # local-path-provisioner volumes: PostgreSQL, rustfs snapshots, job data.
        storage = {
          size = node.diskLayout.storage;
          content = ext4 "/var/lib/rancher/k3s/storage";
        };
      };
    };
  };

  boot.loader =
    if bios then
      {
        grub = {
          enable = true;
          configurationLimit = 5;
        };
      }
    else
      {
        systemd-boot = {
          enable = true;
          configurationLimit = 5;
        };
        efi.canTouchEfiVariables = true;
      };
}
