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

`flake.nix` pins cosign 3.x from nixpkgs unstable, so no system install is
needed: `nix run .#cosign -- <args>` runs it, `nix develop` puts it on `PATH`.
CI installs Nix and takes cosign from the same flake, so local and CI builds
run the identical binary; `flake.lock` is the only place the version is
pinned, and `nix flake update` bumps it for both.

Generate the keypair once, from the project root:

```sh
nix run .#cosign -- generate-key-pair
cp cosign.pub container/cosign.pub
```

Commit `container/cosign.pub` and add two GitHub Actions secrets from
`cosign.key`:

- `COSIGN_PRIVATE_KEY` — the contents of `cosign.key`
- `COSIGN_PASSWORD` — the passphrase you set

Add them as **environment** secrets, not repository secrets: create an
environment named `release` (Settings > Environments), restrict its
deployment branches to `main`, and put the two secrets there. The workflow
declares `environment: release`, so a workflow edited on any other branch and
started with `workflow_dispatch` cannot read the key. Combine this with branch
protection on `main` (required review, no force-push) so a single account
cannot change the workflow and sign with the key in one step.

`cosign.key` is gitignored and must never be committed.

CI refuses to push if `COSIGN_PRIVATE_KEY` does not match the committed
`container/cosign.pub`, so a key mix-up fails before `latest` is touched.

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
nix run .#cosign -- verify --key cosign.pub ghcr.io/OWNER/REPO:latest
```

Every signature is also recorded in the public Rekor transparency log. The
containers stack only checks the key (the policy has no `rekorPublicKey`), so
the log is not needed for verification; it exists so that every use of the
key is auditable. If you ever see a Rekor entry for this key that CI did not
make, treat the key as compromised (see below). Images signed before the log
was enabled need `--insecure-ignore-tlog=true` to verify.

### cosign 3 flags

`build.sh` and CI sign with two flags that turn off cosign 3 defaults:

```sh
cosign sign --yes --key cosign.key \
  --use-signing-config=false --new-bundle-format=false \
  ghcr.io/OWNER/REPO@sha256:...
```

`--yes` accepts the Rekor immutable-record terms that cosign otherwise asks
for interactively (the entry is the image digest and the public key). The
"`--new-bundle-format` has been deprecated" warning is expected; see below.

- `--new-bundle-format=false` keeps the signature as a legacy
  `sha256-<digest>.sig` attachment. cosign 3 defaults this to true and stores
  the signature as an OCI 1.1 referring artifact instead, which skopeo (and so
  `rpm-ostree`/`bootc`) does not look for: the rebase then fails with
  "A signature was required, but no signature exists".
- `--use-signing-config=false` disables the TUF-provided signing config that
  cosign 3 enables by default, so the legacy attachment and the public Rekor
  instance are used.

Verification needs neither flag — cosign 3 reads legacy attachments fine.

### If the key is compromised

There is no key expiry and no revocation: anything signed with the key is
accepted by every installed system until the key in
`/etc/pki/containers/cosign.pub` is replaced. The replacement has to come from
a signed image, which the attacker can also produce, so act quickly and tell
users to switch by digest:

1. Rotate the GitHub secrets and the GHCR credentials of every account with
   write access; review the Rekor log and GHCR for signatures/pushes CI did not
   make, and delete the affected tags.
2. Generate a new keypair, commit the new `container/cosign.pub`, update the
   secrets, and run the workflow.
3. Publish the new image digest out of band (release notes, issue). Users
   install the new key manually and rebase to that digest with the signed
   transport:

   ```sh
   sudo cp cosign.pub /etc/pki/containers/cosign.pub
   rpm-ostree rebase ostree-image-signed:docker://ghcr.io/OWNER/REPO@sha256:...
   ```

   After the reboot the image's key and policy are in place and normal
   `rpm-ostree upgrade` resumes.

cosign 3.1 warns that `--new-bundle-format` is deprecated and that the new
format will eventually be the only one, so this setup needs revisiting once
the containers stack reads OCI 1.1 referring artifacts.

## CI

`.github/workflows/publish.yml` builds, pushes, and signs the image:

- Runs daily at 04:00 UTC and on demand (`workflow_dispatch`). It has no
  `pull_request` trigger on purpose: nothing from a fork ever runs with the
  `packages: write` token or the signing key. Do not add `pull_request_target`.
- `--pull=always` rebuilds on the newest `cosmic-atomic:45` base each run. The
  base image is pulled with the runner's default (accept-anything) policy, so
  whatever Fedora's `fedora-ostree-desktops` account publishes is trusted and
  signed as-is; the signature attests "built by this CI", not "unchanged from
  Fedora".
- Installs Nix and builds `.#cosign` from the flake, so the cosign binary is
  the one `flake.lock` pins — the same one `nix develop` gives you locally.
  The store path comes from cache.nixos.org and is signature-checked by Nix.
- Runs `nix flake check` (see "Tests") before building.
- Checks that the private key matches `container/cosign.pub` and that the
  policy inside the image requires a signature for exactly `IMAGE_REPO`
  before anything is pushed.
- Adds the package list label (see "Checking package changes before
  upgrading") before the push, so the signature covers it.
- Pushes to `ghcr.io/<owner>/<repo>`, signs the pushed digest, uploads the
  signature to Rekor, and verifies it.
- Actions are pinned to commit SHAs and Dependabot (`.github/dependabot.yml`)
  opens PRs to bump them; review the diff of the action, not just the version,
  before merging. The secrets live in the `release` environment (see above).
- The job needs `bash` (`${VAR,,}` and `<(...)`) and `jq`, both present on
  `ubuntu-latest`.

`.github/workflows/test.yml` runs the tests and shellcheck (`nix flake
check`, see "Tests") on every push, every pull request, and on demand. It is
separate from the publish workflow: it builds no image, has only `contents: read`, and
uses no environment or secrets, so running it on pull requests from forks is
safe. It uses `pull_request`; never change that to `pull_request_target`. A
new push to the same branch or PR cancels the run in progress. To block
merging when it fails, make the `test` check required in the branch
protection rule for `main`.

## Tests

`tests/test_package_diff.py` covers `cosmic-atomic-nix-diff` and
`scripts/add-package-label.sh`. `skopeo`, `cosign`, `rpm-ostree` and `podman`
are replaced by fake commands, so no network, registry or container runtime is
needed. The tests cover:

- the package diff output, including install-only packages like `kernel`;
- which image is inspected and that cosign verifies exactly the inspected
  digest, before the label is decoded;
- the safety checks: failed signatures, gzip bombs, malformed or truncated
  gzip, invalid digests, invalid package lines, and escape sequences;
- that the label script writes a deterministic label the diff tool can read,
  rebuilds with `--pull=never`, and rejects bad lists before building;
- that the Containerfile, the label script and the diff tool agree on the
  label name, list path and line format.

Run them, plus shellcheck on `scripts/`, with:

```sh
nix flake check -L                                   # sandboxed, as CI does
nix develop -c python3 -m unittest discover -s tests -v   # quick, in place
```

The flake only sees files tracked by git, so `git add` new test files before
running `nix flake check`.

## Building locally

`build.sh` calls `cosign` from `PATH`, so run it inside the dev shell:

```sh
nix develop                 # cosign on PATH
sudo podman login ghcr.io   # once, for the push
cosign login ghcr.io        # once, for signing
IMAGE_REPO=ghcr.io/OWNER/REPO scripts/build.sh
```

`build.sh` builds, pushes, signs the pushed digest, and prints the rebase
command. `podman` runs under `sudo` so the image lands in root's container
storage, which is where `rpm-ostree` looks for `containers-storage:` images;
`rpm-ostree` itself does not need `sudo` (it authorizes through polkit).

To build without pushing or signing (only `container/cosign.pub` is needed):

```sh
IMAGE_REPO=ghcr.io/OWNER/REPO scripts/build.sh --no-push
rpm-ostree rebase ostree-unverified-image:containers-storage:ghcr.io/OWNER/REPO:latest
```

## Installing / switching to the image

The policy and public key live inside the image, so the first install is a
bootstrap: the image you install decides which key is trusted for every
update after it. Anyone with push access to the GHCR package (but not the
signing key) can overwrite the `latest` tag, so do not install an unverified
`latest`. Verify a digest first and install exactly that digest.

Get `cosign.pub` from this repository (check it against the key in the image
later), then:

```sh
nix run .#cosign -- verify --key cosign.pub ghcr.io/OWNER/REPO:latest
```

The output includes the verified digest. Rebase to that digest, not the tag;
the `ostree-unverified-image` transport skips signature checks but a digest
reference can only resolve to the content you just verified:

```sh
rpm-ostree rebase ostree-unverified-image:docker://ghcr.io/OWNER/REPO@sha256:...
sudo systemctl reboot
```

Then switch to the verified transport, which is what all later upgrades use:

```sh
diff cosign.pub /etc/pki/containers/cosign.pub   # must be identical
rpm-ostree rebase ostree-image-signed:docker://ghcr.io/OWNER/REPO:latest
sudo systemctl reboot
```

Alternatively, put `cosign.pub` at `/etc/pki/containers/cosign.pub` and the
merged policy (`/etc/containers/policy.json` from the image, or the fragment
merged with `merge-policy.py`) on the host by hand before the first rebase;
then the first step can use `ostree-image-signed:` directly and the
unverified transport is never used.

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

Note what this does and does not verify: only the `ghcr.io/OWNER/REPO` scope
requires a signature. `ostree-image-signed:docker://` pointed at **any other**
repository falls through to the `docker` transport's accept-anything rule and
installs unsigned content while looking verified. The signed transport is
only meaningful for this image's repository; for anything else, add a scope
to `/etc/containers/policy.json` first.

Later updates are verified automatically:

```sh
rpm-ostree upgrade
sudo systemctl reboot
```

## Checking package changes before upgrading

For container images, `rpm-ostree upgrade --preview` only lists added and
removed layers, not package versions: the RPM database is inside the layers,
and the preview only downloads the manifest. This image works around that:

- The build writes the image's package list (`name.arch<TAB>[epoch:]version-release`)
  to `/usr/share/cosmic-atomic-nix/packages.tsv`.
- `scripts/add-package-label.sh` then copies the same list, gzipped and
  base64-encoded, into the `io.github.cosmic-atomic-nix.packages` image label
  (about 20–30 KB). Labels live in the image config, so `skopeo inspect`
  reads them without downloading any layers. The label is added before the
  push, so it is covered by the signature.

On the host:

```sh
cosmic-atomic-nix-diff                              # the image the booted deployment follows
cosmic-atomic-nix-diff ghcr.io/OWNER/REPO:latest    # or any tag
```

It compares the booted deployment's `packages.tsv` with the label of the
remote image and prints changed, removed, and added packages. Only the
manifest and config are downloaded. Packages layered locally with
`rpm-ostree install` are not in either list. Images built before this feature
have no label and the tool says so. If the booted deployment is pinned to a
digest, pass a tag.

The label comes from the registry and is treated as untrusted:

- **Signature.** `skopeo inspect` does not check signatures, so the tool runs
  `cosign verify --key /etc/pki/containers/cosign.pub REPO@DIGEST` on the
  digest it inspected, before decoding the label. A failed verification is an
  error and nothing is shown. cosign 3 checks the Rekor entry by default, so
  verification needs network access to Sigstore and caches its trust root in
  `~/.sigstore`.
- **Where cosign comes from.** `cosign` on `PATH` if there is one, otherwise
  `nix build --no-link --print-out-paths nixpkgs#cosign`. The tool builds and
  then runs the binary as separate steps, instead of `nix run`, so a Nix
  failure is never mistaken for a bad signature. The first run downloads
  nixpkgs and cosign (this takes a while); later runs reuse the Nix store
  until it is garbage-collected. `nixpkgs#cosign` follows the flake
  registry's `nixpkgs` (nixpkgs-unstable by default) and is not pinned by
  this repo's `flake.lock`; it comes from cache.nixos.org, signature-checked
  by Nix. If neither works (no Nix, offline, build failed), the diff is
  still shown but marked `NOT VERIFIED`, with the reason and the command to
  check it by hand.
- **Size.** The label may be at most 4 MiB encoded and 8 MiB decompressed
  (a real one is about 30 KB / 80 KB), so a gzip bomb cannot exhaust memory.
- **Content.** Every line must match the format `rpm` writes
  (`name.arch<TAB>[epoch:]version-release`, rpm's allowed characters only);
  otherwise the whole list is rejected. Other strings from the registry, like
  the creation date, have non-printable characters escaped, so a forged label
  cannot inject terminal escape sequences. `add-package-label.sh` applies the
  same format check at build time.

`rpm-ostree upgrade` follows the tag, so if CI pushes again between the check
and the upgrade you get a newer (still signed) image than the one you
reviewed. To install exactly what was checked, rebase to the printed digest:
`rpm-ostree rebase ostree-image-signed:docker://ghcr.io/OWNER/REPO@sha256:...`.

## Layout

```
.github/workflows/publish.yml # daily + on-demand build, push, sign
.github/workflows/test.yml   # tests + shellcheck on push and pull request
.github/dependabot.yml       # PRs to bump the SHA-pinned actions
flake.nix                    # cosign from nixpkgs, test checks (nix flake check)
container/
  Containerfile              # image definition
  cosign.pub                 # trusted public key (generated, committed)
  containers-policy.json     # signature rule for IMAGE_REPO (fragment)
  merge-policy.py            # merges the rule into the base policy.json at build
  registries.d.yaml          # cosign attachment lookup for ghcr.io
  nix.mount                  # bind mount /var/nix -> /nix
  nix-var-dir.service        # create /var/nix at boot
  nix-tmpfiles.conf          # store/profile/gcroots directories
  cosmic-atomic-nix-diff     # host tool: package diff vs. a newer image
scripts/
  build.sh                   # build, push, sign locally
  add-package-label.sh       # copy packages.tsv into an image label
tests/
  test_package_diff.py       # diff tool + label script tests (mocked externals)
```

## Additional Info

- [Unofficial CI-Test Builds (OCI Image)](https://gitlab.com/fedora/ostree/ci-test)
- [Configuration used to build the Fedora Atomic Desktops](https://forge.fedoraproject.org/atomic-desktops/config)
