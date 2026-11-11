#!/usr/bin/env bash
set -euo pipefail

REGISTRY=ghcr.io/enki-farm
IMAGE=hermes-agent
IMAGE_TAG="${IMAGE_TAG:-v0.23.1-herdr-core-v0.1.20-aigateway1}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
REFERENCE="$REGISTRY/$IMAGE:$IMAGE_TAG"

command -v docker >/dev/null || { printf 'docker is required\n' >&2; exit 1; }
docker buildx version >/dev/null

if docker buildx imagetools inspect "$REFERENCE" >/dev/null 2>&1; then
  printf 'Already published; leaving immutable tag unchanged: %s\n' "$REFERENCE"
  exit 0
fi

docker buildx build \
  --platform "$PLATFORMS" \
  --label "org.opencontainers.image.base.name=nousresearch/hermes-agent:v0.23.1" \
  --label "org.opencontainers.image.version=$IMAGE_TAG" \
  --tag "$REFERENCE" \
  --push \
  docker/hermes-agent

printf 'Published %s\n' "$REFERENCE"