# cosmic-atomic-nix

A bootc/OCI container image based on Fedora COSMIC Atomic 45 with the Nix
package manager added.

## What it is

The image starts from `quay.io/fedora-ostree-desktops/cosmic-atomic:45` and
installs Nix from Fedora's RPMs (`nix`, `nix-daemon`). It is meant to be built
with `podman` and rebased onto with `rpm-ostree`, giving an immutable Fedora
desktop that also has Nix available.

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

## Layout

```
container/
  Containerfile          # image definition
  nix.mount              # bind mount /var/nix -> /nix
  nix-var-dir.service    # create /var/nix at boot
  nix-tmpfiles.conf      # store/profile/gcroots directories
scripts/
  build.sh               # build the image with podman
```

## Building

Run from the project root:

```sh
scripts/build.sh
```

This runs:

```sh
sudo podman build --pull=always -t localhost/fedora/45/cosmic-atomic-nix:latest container/
```

`sudo` is needed so `rpm-ostree` can access the image in root's container
storage.

## Installing / switching to the image

```sh
sudo rpm-ostree rebase ostree-unverified-image:containers-storage:localhost/fedora/45/cosmic-atomic-nix:latest
sudo systemctl reboot
```

To pick up later builds:

```sh
sudo rpm-ostree upgrade
sudo systemctl reboot
```
