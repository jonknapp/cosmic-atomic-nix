# cosmic-atomic-nix

A bootc/OCI container image based on Fedora COSMIC Atomic 45 with the Nix
package manager added.

## What it is

The image starts from `quay.io/fedora-ostree-desktops/cosmic-atomic:45` and
installs Nix from Fedora's RPMs (`nix`, `nix-daemon`). It is built with
`podman`, published to GitHub Container Registry, signed with cosign, and
rebased onto with `rpm-ostree`, giving an immutable Fedora desktop that also has
Nix available.

## How it works

The tricky part of adding Nix to an atomic system is `/nix`. On an ostree/bootc
system `/nix` is read-only, but Nix needs a writable store.

Instead of using `/nix` directly, the image stores Nix data in `/var/nix`
(which is writable and persists across updates) and bind-mounts it onto `/nix`
at boot:

- `container/nix-var-dir.service` — creates `/var/nix` before mounting.
- `container/nix.mount` — bind-mounts `/var/nix` onto `/nix` before
  `local-fs.target`.
- `container/nix-tmpfiles.conf` — creates the store, profiles, and gcroots
  directories with the correct ownership/permissions.
- SELinux contexts are set so `/nix` is labeled like `/usr` (so Nix-built
  binaries can execute), while `/nix/var/nix/daemon-socket` is labeled
  `var_run_t` so systemd can create the socket.

The `nix-daemon.socket` is enabled by the RPMs, and `bootc container lint`
validates the resulting image.

## Signing and verification

Images are signed with cosign using a keypair. The private key stays offline (a
GitHub secret); the public key is committed and baked into the image.

`flake.nix` pins cosign from nixpkgs unstable, so no system install is needed:
`nix run .#cosign -- <args>` runs it, `nix develop` puts it on `PATH`.

Generate the keypair once, from the project root:

```sh
nix run .#cosign -- generate-key-pair
cp cosign.pub container/cosign.pub
```

Commit `container/cosign.pub` and add two GitHub Actions secrets from
`cosign.key`:

- `COSIGN_PRIVATE_KEY` — the contents of `cosign.key`
- `COSIGN_PASSWORD` — the passphrase you set

`cosign.key` is gitignored and must never be committed.

The image ships:

- `/etc/pki/containers/cosign.pub` — the trusted verification key.
- `/etc/containers/policy.json` — the base image's policy with one rule added:
  a valid signature is required for the published OS repository. Fedora's
  existing entries (default and Red Hat registry rules) are kept. Fedora 45
  ships its default policy as `/usr/share/containers/policy.json` and no
  longer creates the `/etc` copy, so `merge-policy.py` reads whichever of the
  two exists and always writes `/etc/containers/policy.json`.
- `/etc/containers/registries.d/cosmic-atomic-nix.yaml` — enables reading
  cosign signature attachments.

Verify manually with:

```sh
nix run .#cosign -- verify --key cosign.pub --insecure-ignore-tlog=true \
  ghcr.io/OWNER/REPO:latest
```

Images are signed without a transparency log entry (`--tlog-upload=false`),
so `--insecure-ignore-tlog=true` is needed to skip the Rekor lookup.

### cosign 3 flags

`build.sh` and CI sign with three flags that look redundant but are not:

```sh
cosign sign --key cosign.key \
  --tlog-upload=false --use-signing-config=false --new-bundle-format=false \
  ghcr.io/OWNER/REPO@sha256:...
```

- `--new-bundle-format=false` keeps the signature as a legacy
  `sha256-<digest>.sig` attachment. cosign 3 defaults this to true and stores
  the signature as an OCI 1.1 referring artifact instead, which skopeo (and so
  `rpm-ostree`/`bootc`) does not look for: the rebase then fails with
  "A signature was required, but no signature exists".
- `--use-signing-config=false` disables the TUF-provided signing config that
  cosign 3 enables by default; with it on, cosign rejects `--tlog-upload=false`.
- Both default to false on cosign 2.6.x, so the same command works there and
  in CI, which pins cosign v2.6.2.

Verification needs neither flag — cosign 3 reads legacy attachments fine.

cosign 3.1 warns that `--new-bundle-format` is deprecated and that the new
format will eventually be the only one, so this setup needs revisiting once
the containers stack reads OCI 1.1 referring artifacts.

## CI

`.github/workflows/build.yml` builds, pushes, and signs the image:

- Runs daily at 04:00 UTC and on demand (`workflow_dispatch`).
- `--pull=always` rebuilds on the newest `cosmic-atomic:45` base each run.
- Pushes to `ghcr.io/<owner>/<repo>` and signs the pushed digest.

## Building locally

`build.sh` calls `cosign` from `PATH`, so run it inside the dev shell:

```sh
nix develop                 # cosign on PATH
sudo podman login ghcr.io   # once, for the push
cosign login ghcr.io        # once, for signing
IMAGE_REPO=ghcr.io/OWNER/REPO scripts/build.sh
```

`build.sh` builds, pushes, signs the pushed digest, and prints the rebase
command. `sudo` is needed so `rpm-ostree` can use the image from root's
container storage.

To build without pushing or signing (only `container/cosign.pub` is needed):

```sh
IMAGE_REPO=ghcr.io/OWNER/REPO scripts/build.sh --no-push
sudo rpm-ostree rebase ostree-unverified-image:containers-storage:ghcr.io/OWNER/REPO:latest
```

## Installing / switching to the image

The policy and public key live inside the image, so bootstrap in two steps:
first install unsigned to get them onto the host, then switch to the verified
transport.

```sh
sudo rpm-ostree rebase ostree-unverified-image:docker://ghcr.io/OWNER/REPO:latest
sudo systemctl reboot

sudo rpm-ostree rebase ostree-image-signed:docker://ghcr.io/OWNER/REPO:latest
sudo systemctl reboot
```

`/etc` goes through ostree's 3-way merge. If `/etc/containers/policy.json`
was edited locally on the host, the image's version is not applied and the
signed rebase fails. The image's pristine copy is in `/usr/etc`, so after
booting the unsigned image, reset the live file to it:

```sh
sudo cp /usr/etc/containers/policy.json /etc/containers/policy.json
```

The signed rebase also requires the policy's top-level `default` to be
something other than `insecureAcceptAnything`; otherwise it fails with
`containers-policy.json specifies a default of insecureAcceptAnything;
refusing usage`. The image sets it to `reject`, and accepts anything else only
for the `docker` (registry pulls) and `containers-storage` (local images)
transports. Other transports, like `podman load` from `docker-archive:` or
`oci-archive:`, are rejected. Rootless podman reads
`~/.config/containers/policy.json` in place of the system file if it exists,
so create one if you need those. Check with
`grep -A3 '"default"' /etc/containers/policy.json` before the second rebase.

Later updates are verified automatically:

```sh
sudo rpm-ostree upgrade
sudo systemctl reboot
```

## Layout

```
.github/workflows/build.yml  # daily + on-demand build, push, sign
flake.nix                    # cosign from nixpkgs (nix run .#cosign, nix develop)
container/
  Containerfile              # image definition
  cosign.pub                 # trusted public key (generated, committed)
  containers-policy.json     # signature rule for IMAGE_REPO (fragment)
  merge-policy.py            # merges the rule into the base policy.json at build
  registries.d.yaml          # cosign attachment lookup for ghcr.io
  nix.mount                  # bind mount /var/nix -> /nix
  nix-var-dir.service        # create /var/nix at boot
  nix-tmpfiles.conf          # store/profile/gcroots directories
scripts/
  build.sh                   # build, push, sign locally
```
