#!/usr/bin/env bash
set -euo pipefail

# Local build only. Publishing is an explicit, separate operation.
image_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd -- "${image_dir}/.." && pwd)"

base_image="${BASE_IMAGE:-python:slim}"
platform="${PLATFORM:-linux/amd64}"
tag="${LOCAL_IMAGE:-${GHCR_IMAGE:-web-terminal}:${IMAGE_VERSION:-0.1.0}}"
if [[ "$platform" != "linux/amd64" ]]; then
  echo "Only linux/amd64 is qualified for this initial delivery." >&2
  exit 2
fi
command -v docker >/dev/null

docker buildx build \
  --file "${image_dir}/Dockerfile" \
  --platform "$platform" \
  --build-arg "BASE_IMAGE=${base_image}" \
  --build-arg "GO_IMAGE=${GO_IMAGE:-golang:1.26.5-bookworm}" \
  --build-arg "CODEX_EXPECTED_VERSION=${CODEX_EXPECTED_VERSION:-}" \
  --tag "$tag" \
  --label "org.opencontainers.image.source=${SOURCE_REPOSITORY:-}" \
  --label "org.opencontainers.image.version=${IMAGE_VERSION:-0.1.0}" \
  --load \
  "$project_dir"

echo "Built locally: ${tag} (not pushed). Existing copy-in binary is unchanged."
echo "Next: LOCAL_IMAGE=${tag} bash ${image_dir}/test_web_terminal_image.sh"
