# Lean headless base OS: static network from the captured DHCP lease, admin user, sshd (Req 3, 4, 17.1).
{
  lib,
  pkgs,
  modulesPath,
  node,
  ...
}:
{
  # minimal.nix drops docs, fonts, xdg and default packages. profiles/headless.nix is deliberately
  # not imported: it panics on boot failure and removes the console getty, and on a Proxmox VM
  # the console's emergency shell is the last-resort debugging path.
  imports = [
    (modulesPath + "/profiles/minimal.nix")
    (modulesPath + "/profiles/qemu-guest.nix")
  ];

  networking.hostName = node.hostName;

  # The DHCP lease seen on the ISO, pinned statically; matched by MAC, not interface name.
  networking.useDHCP = false;
  networking.useNetworkd = true;
  systemd.network.networks."10-primary" = {
    matchConfig.MACAddress = node.network.mac;
    address = [ node.network.address ];
    gateway = [ node.network.gateway ];
    dns = node.network.dns;
    linkConfig.RequiredForOnline = "routable";
  };
  services.resolved.enable = true;

  users.mutableUsers = false;
  users.users.${node.admin.user} = {
    isNormalUser = true;
    extraGroups = [ "wheel" ];
    openssh.authorizedKeys.keys = node.admin.sshKeys;
  };
  security.sudo.wheelNeedsPassword = false;

  services.openssh = {
    enable = true;
    settings = {
      PasswordAuthentication = false;
      KbdInteractiveAuthentication = false;
      PermitRootLogin = "no";
    };
  };

  services.qemuGuest.enable = true;

  # Desktop-oriented services that minimal.nix leaves on.
  security.polkit.enable = false;
  services.udisks2.enable = false;
  fonts.fontconfig.enable = false;
  documentation.enable = false;

  services.journald.extraConfig = ''
    SystemMaxUse=200M
    RuntimeMaxUse=50M
  '';

  nix = {
    settings = {
      experimental-features = [
        "nix-command"
        "flakes"
      ];
      trusted-users = [ "@wheel" ];
      auto-optimise-store = true;
    };
    gc = {
      automatic = true;
      dates = "weekly";
      options = "--delete-older-than 14d";
    };
  };

  environment.systemPackages = with pkgs; [
    htop
    lvm2
  ];

  time.timeZone = "UTC";
  system.stateVersion = "26.05";
}
