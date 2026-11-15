#!/usr/bin/env bash
set -u

# sudo needed for rpm-ostree to allow the local container
sudo podman build --pull=always -t localhost/fedora/45/cosmic-atomic-nix:latest container/
