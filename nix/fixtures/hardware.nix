# Stand-in for the hardware-configuration.nix that nixos-generate-config writes on a real host.
{ modulesPath, ... }:
{
  imports = [ (modulesPath + "/profiles/qemu-guest.nix") ];
  boot.initrd.availableKernelModules = [ "virtio_pci" "virtio_scsi" "sd_mod" "sr_mod" ];
  nixpkgs.hostPlatform = "x86_64-linux";
}
