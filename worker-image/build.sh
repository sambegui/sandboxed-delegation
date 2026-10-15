#!/usr/bin/env bash
# Build the worker image into the CURRENT user's rootless Podman store. Run it as the user that owns your sandbox gateway:
#   bash build.sh [context-dir] [tag]          # defaults: this directory, localhost/omp-worker:18.9.2-1
# Disk is checked first and capped: the toolchains make the image several GB.
set -euo pipefail
cd /tmp
CTX="$(cd "${1:-$(dirname "${BASH_SOURCE[0]}")}" && pwd)"
TAG="${2:-localhost/omp-worker:18.9.2-1}"
CAP_GB=10
root="$(podman info --format '{{.Store.GraphRoot}}')"
avail_gb="$(df -BG --output=avail "$root" | tail -1 | tr -dc '0-9')"
echo "== $(hostname) $(whoami) store=$root avail=${avail_gb}G cap=${CAP_GB}G"
[ "$avail_gb" -ge $((CAP_GB + 10)) ] || {
  echo "refusing: less than $((CAP_GB + 10))G free"
  exit 3
}
podman build --pull=missing ${NO_CACHE:+--no-cache} -t "$TAG" -f "$CTX/Dockerfile" "$CTX"
echo "== built:"
podman images --digests --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}' | grep -F "${TAG%%:*}"
# The manifest is long (every apt package): read it whole, then print the head and the natives digests only.
echo "== manifest (full copy: podman run --rm --network none --entrypoint cat $TAG /opt/worker-manifest.txt):"
manifest="$(podman run --rm --network none --entrypoint cat "$TAG" /opt/worker-manifest.txt)"
printf '%s\n' "$manifest" | sed -n '1,5p;/^--- natives/,$p'
echo BUILD_DONE
