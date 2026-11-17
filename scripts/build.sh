#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: IMAGE_REPO=ghcr.io/you/cosmic-atomic-nix $0 [--no-push]" >&2
}

push=1
for arg in "$@"; do
  case "${arg}" in
    --no-push) push=0 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 1 ;;
  esac
done

# Full image reference the build is published to.
image_repo="${IMAGE_REPO:-ghcr.io/CHANGE_ME/cosmic-atomic-nix}"
tag="${TAG:-latest}"
image="${image_repo}:${tag}"

if [[ "${image_repo}" == *CHANGE_ME* ]]; then
  echo "Set IMAGE_REPO, e.g. IMAGE_REPO=ghcr.io/you/cosmic-atomic-nix $0" >&2
  exit 1
fi

# The build copies container/cosign.pub into the image, so it is always needed.
if [[ ! -f container/cosign.pub ]]; then
  echo "Missing container/cosign.pub. Run 'nix run .#cosign -- generate-key-pair', then copy cosign.pub to container/." >&2
  exit 1
fi

if [[ "${push}" -eq 1 && ! -f cosign.key ]]; then
  echo "Missing cosign.key in the project root (needed to sign). Use --no-push to build only." >&2
  exit 1
fi

# sudo is needed so rpm-ostree can use the image from root's container storage.
sudo podman build --pull=always \
  --build-arg "IMAGE_REPO=${image_repo}" \
  -t "${image}" container/

# Copy the package list into a label for cosmic-atomic-nix-diff. Runs before
# the push so the signed digest covers the label.
sudo "$(dirname "$0")/add-package-label.sh" "${image}"

if [[ "${push}" -eq 0 ]]; then
  printf '\nBuilt %s (not pushed or signed)\n' "${image}"
  printf 'Rebase the host with:\n  rpm-ostree rebase ostree-unverified-image:containers-storage:%s\n' "${image}"
  exit 0
fi

digestfile="$(mktemp)"
trap 'rm -f "${digestfile}"' EXIT

# Log in first, once: sudo podman login ghcr.io
sudo podman push "${image}" --digestfile "${digestfile}"
digest="$(cat "${digestfile}")"

# Signs the pushed digest and records the signature in the public Rekor
# transparency log, so every use of the key is auditable. --yes accepts
# Rekor's immutable-record terms non-interactively (the entry contains the
# image digest and public key, nothing personal). Log in first, once:
# cosign login ghcr.io
#
# cosign 3 turned on the new bundle format and the TUF signing config by
# default. Both are turned back off here: the signature has to be a legacy
# `sha256-<digest>.sig` attachment for skopeo/bootc to find it. CI runs this
# same cosign (from the flake) with the same flags.
cosign sign --yes --key cosign.key \
  --use-signing-config=false --new-bundle-format=false \
  "${image_repo}@${digest}"
cosign verify --key container/cosign.pub "${image_repo}@${digest}"

printf '\nSigned %s@%s\n' "${image_repo}" "${digest}"
printf 'Rebase the host with:\n  rpm-ostree rebase ostree-image-signed:docker://%s:%s\n' "${image_repo}" "${tag}"
