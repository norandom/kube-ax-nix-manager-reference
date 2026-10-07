{
  description = "kube-ax: NixOS nodes for a small, efficiency-tuned k3s + Agent Substrate/AX cluster";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    disko = {
      url = "github:nix-community/disko";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    { nixpkgs, disko, ... }:
    let
      inherit (nixpkgs) lib;

      # One configuration for every node; differences come only from node.json (rendered by
      # lib/nodeconfig.py). On a node this flake lives at /etc/nixos and is used as
      # `path:/etc/nixos#node`, next to the host-generated hardware-configuration.nix.
      # hardware-configuration.nix is generated on the host after disko has partitioned the disk,
      # so the config must also evaluate without it (disko reads only config.disko.devices).
      hostHardware = lib.optional (builtins.pathExists ./hardware-configuration.nix) ./hardware-configuration.nix;

      mkNode =
        { node, hardware ? hostHardware }:
        lib.nixosSystem {
          system = "x86_64-linux";
          specialArgs = { inherit node; };
          modules = [
            disko.nixosModules.disko
            ./disko.nix
          ]
          ++ lib.toList hardware
          ++ [
            ./modules/base.nix
            ./modules/tuning.nix
            ./modules/k3s.nix
          ];
        };

      readJSON = path: builtins.fromJSON (builtins.readFile path);

      # Fixtures exercise every branch (server/agent, bios/uefi, swap modes, CPU manager) for
      # `just nix-check`, with a stand-in for the hardware config only a real host can generate.
      fixtures = lib.mapAttrs' (
        file: _:
        lib.nameValuePair "fixture-${lib.removeSuffix ".json" file}" (mkNode {
          node = readJSON (./fixtures + "/${file}");
          hardware = ./fixtures/hardware.nix;
        })
      ) (lib.filterAttrs (f: _: lib.hasSuffix ".json" f) (builtins.readDir ./fixtures));
    in
    {
      # Same pinned disko the node config uses; install runs it from the uploaded flake.
      packages.x86_64-linux.disko = disko.packages.x86_64-linux.disko;

      nixosConfigurations =
        fixtures
        // lib.optionalAttrs (builtins.pathExists ./node.json) {
          node = mkNode { node = readJSON ./node.json; };
        };
    };
}
