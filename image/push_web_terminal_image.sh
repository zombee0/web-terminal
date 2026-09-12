#!/usr/bin/env bash
set -euo pipefail
: "${LOCAL_IMAGE:=web-terminal:0.1.0}"
: "${GHCR_IMAGE:?Set GHCR_IMAGE=ghcr.io/owner/web-terminal (lowercase)}"
: "${IMAGE_VERSION:?Set IMAGE_VERSION=0.1.0}"
if [[ ! "$GHCR_IMAGE" =~ ^ghcr\.io/[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._/-]*$ ]] ||
   [[ ! "$IMAGE_VERSION" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$ ]]; then
  echo "Invalid GHCR repository or image tag." >&2
  exit 2
fi
publish_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export LOCAL_IMAGE
bash "$publish_dir/test_web_terminal_image.sh"
publish_tag="${GHCR_IMAGE}:${IMAGE_VERSION}"
docker tag "$LOCAL_IMAGE" "$publish_tag"
# Uses the builder's existing docker login, never asks the platform for a PAT.
docker push "$publish_tag"
echo "Pushed ${publish_tag}. Make the GHCR package public before platform import."
docker buildx imagetools inspect "$publish_tag"
echo "Use the registry Digest above, not the local image ID, in agent.market.yaml."
