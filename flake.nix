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

      # Runs shellcheck on scripts/ and the Python tests, from the repo root
      # found by walking up from the current directory. Shared by `nix run
      # .#test` (your working copy, never cached) and `nix flake check`
      # (sandboxed copy of the tracked files, cached until inputs change).
      # Extra arguments go to unittest, e.g. `-k gzip`.
      testRunner =
        pkgs:
        pkgs.writeShellApplication {
          name = "cosmic-atomic-nix-test";
          # bash, coreutils, gnugrep, gzip and diffutils are needed by
          # scripts/add-package-label.sh, which the tests run.
          runtimeInputs = [
            pkgs.python3
            pkgs.shellcheck
            pkgs.bash
            pkgs.coreutils
            pkgs.gnugrep
            pkgs.gzip
            pkgs.diffutils
          ];
          text = ''
            root="$PWD"
            while [[ ! -f "$root/tests/test_package_diff.py" ]]; do
              if [[ "$root" == / ]]; then
                echo "Run this from inside the cosmic-atomic-nix repository." >&2
                exit 1
              fi
              root="$(dirname "$root")"
            done
            cd "$root"

            echo "== shellcheck scripts/*.sh"
            shellcheck scripts/*.sh
            echo "== python3 -m unittest discover -s tests"
            PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v "$@"
          '';
        };
    in
    {
      # cosign 3.x, used for key generation, signing and verification:
      #   nix run .#cosign -- version
      packages = forAllSystems (pkgs: {
        cosign = pkgs.cosign;
        default = pkgs.cosign;
      });

      # Tests for the package diff tool and the label script, plus shellcheck,
      # run on the working copy every time (untracked files included):
      #   nix run .#test
      #   nix run .#test -- -k gzip     # only tests matching "gzip"
      apps = forAllSystems (pkgs: {
        test = {
          type = "app";
          program = "${testRunner pkgs}/bin/cosmic-atomic-nix-test";
          meta.description = "Run shellcheck and the Python tests on the working copy";
        };
      });

      # The same tests in the build sandbox:
      #   nix flake check -L
      # External commands (skopeo, cosign, rpm-ostree, podman) are faked, so
      # this needs no network or containers. Nix caches a passing result and
      # only reruns when container/, scripts/, tests/ or flake.lock change;
      # use `nix run .#test` to always run. Only files tracked by git are
      # visible here: `git add` new tests.
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
            }
            ''
              cd "$src"
              ${pkgs.lib.getExe (testRunner pkgs)}
              touch "$out"
            '';
      });

      # Puts cosign on PATH so scripts/build.sh can call it, and the test
      # tools so the tests can also be run by hand:
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
