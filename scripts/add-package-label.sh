#!/usr/bin/env bash
# Copy the image's package list into an image label, so hosts can read it with
# `skopeo inspect` (config blob only) instead of pulling the image. See
# container/cosmic-atomic-nix-diff.
#
# Usage: scripts/add-package-label.sh IMAGE
#
# Rebuilds IMAGE in place as `FROM IMAGE` + `LABEL`, which only changes the
# image config; the layers are unchanged. The label is written through a
# generated Containerfile instead of `podman build --label`, so its size is
# not bound by the 128 KiB limit on a single command-line argument.
set -euo pipefail

# Must match LABEL in container/cosmic-atomic-nix-diff.
label="io.github.cosmic-atomic-nix.packages"
list_path="/usr/share/cosmic-atomic-nix/packages.tsv"

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 IMAGE" >&2
  exit 1
fi
image="$1"

workdir="$(mktemp -d)"
trap 'rm -rf "${workdir}"' EXIT

podman run --rm "${image}" cat "${list_path}" > "${workdir}/packages.tsv"
if [[ ! -s "${workdir}/packages.tsv" ]]; then
  echo "${image} has an empty ${list_path}" >&2
  exit 1
fi

# Same format check as LINE_RE in container/cosmic-atomic-nix-diff, which
# rejects the whole list if any line fails it.
if bad="$(LC_ALL=C grep -Evn $'^[A-Za-z0-9._+^~-]+\t([0-9]+:)?[A-Za-z0-9._+^~]+-[A-Za-z0-9._+^~]+$' \
    "${workdir}/packages.tsv")"; then
  printf 'Unexpected package entries in %s:\n%s\n' "${list_path}" "${bad}" >&2
  exit 1
fi

# -n leaves the file name and timestamp out of the gzip header, so the same
# package list always gives the same label.
encoded="$(gzip -9n < "${workdir}/packages.tsv" | base64 -w0)"

# The base64 alphabet has no quotes, spaces or backslashes, so it is safe
# inside a double-quoted LABEL value.
printf 'FROM %s\nLABEL %s="%s"\n' "${image}" "${label}" "${encoded}" \
  > "${workdir}/Containerfile"
podman build --pull=never -t "${image}" -f "${workdir}/Containerfile" "${workdir}"

# Read the label back and check it decodes to the exact list in the image.
podman image inspect --format "{{index .Labels \"${label}\"}}" "${image}" \
  | base64 -d | gunzip | cmp - "${workdir}/packages.tsv"

printf 'Added %s label to %s (%d packages, %d bytes)\n' \
  "${label}" "${image}" "$(wc -l < "${workdir}/packages.tsv")" "${#encoded}"
