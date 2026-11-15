{
  description = "Tooling for building and signing cosmic-atomic-nix images";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      # cosign 3.x, used for key generation, signing and verification:
      #   nix run .#cosign -- version
      packages = forAllSystems (pkgs: {
        cosign = pkgs.cosign;
        default = pkgs.cosign;
      });

      # Puts cosign on PATH so scripts/build.sh can call it:
      #   nix develop
      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [ pkgs.cosign ];
        };
      });
    };
}
