{
  description = "Debrief: review coding-agent work through the records agents keep";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    # Only the checks use Home Manager; the module works with the one importing it.
    home-manager = {
      url = "github:nix-community/home-manager/release-26.05";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      home-manager,
    }:
    let
      inherit (nixpkgs) lib;
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
        "x86_64-darwin"
      ];
      forAllSystems = f: lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (pkgs: {
        debrief = pkgs.callPackage ./nix/package.nix { };
        default = self.packages.${pkgs.stdenv.hostPlatform.system}.debrief;
      });

      apps = forAllSystems (pkgs: {
        default = {
          type = "app";
          program = lib.getExe self.packages.${pkgs.stdenv.hostPlatform.system}.debrief;
          meta.description = "Run the debrief command";
        };
      });

      overlays.default = final: _prev: {
        debrief = final.callPackage ./nix/package.nix { };
      };

      homeModules = {
        debrief = ./nix/hm-module.nix;
        default = self.homeModules.debrief;
      };
      # The name older Home Manager setups look for.
      homeManagerModules = self.homeModules;

      checks = forAllSystems (pkgs: import ./nix/checks.nix { inherit self pkgs home-manager; });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            pkgs.python3
            pkgs.git
            pkgs.openssl
          ];
        };
      });
    };
}
