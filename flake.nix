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

      # Tests for the package diff tool and the label script, plus shellcheck:
      #   nix flake check
      # External commands (skopeo, cosign, rpm-ostree, podman) are faked, so
      # this runs in the build sandbox without network or containers. Only
      # files tracked by git are visible to the flake: `git add` new tests.
      checks = forAllSystems (pkgs: {
        tests =
          pkgs.runCommand "cosmic-atomic-nix-tests"
            {
              src = pkgs.lib.fileset.toSource {
                root = ./.;
                fileset = pkgs.lib.fileset.unions [
                  ./container
                  ./scripts
                  ./tests
                ];
              };
              # stdenv already provides bash, coreutils, gnugrep, gzip and
              # diffutils, which scripts/add-package-label.sh needs.
              nativeBuildInputs = [
                pkgs.python3
                pkgs.shellcheck
              ];
            }
            ''
              cd "$src"
              shellcheck scripts/*.sh
              python3 -m unittest discover -s tests -v
              touch "$out"
            '';
      });

      # Puts cosign on PATH so scripts/build.sh can call it, and the test
      # tools so the tests can run outside the sandbox:
      #   nix develop
      #   python3 -m unittest discover -s tests -v
      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            pkgs.cosign
            pkgs.python3
            pkgs.shellcheck
          ];
        };
      });
    };
}
