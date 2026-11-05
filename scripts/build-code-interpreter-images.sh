#!/usr/bin/env bash
set -euo pipefail

RELEASE=v1.10.13
COMMIT=2fa9f0d43e8e38b7d6240bc76e2483c6a47c64c7
REGISTRY=ghcr.io/enki-farm
IMAGE_TAG=v1.10.13-arm64
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

command -v git >/dev/null || { printf 'git is required\n' >&2; exit 1; }
command -v docker >/dev/null || { printf 'docker is required\n' >&2; exit 1; }
docker buildx version >/dev/null

SOURCE="$TEMP_DIR/code-interpreter"
git clone --quiet --depth 1 --branch "$RELEASE" \
  https://github.com/LibreChat-AI/code-interpreter.git "$SOURCE"
ACTUAL_COMMIT="$(git -C "$SOURCE" rev-parse HEAD)"
[[ "$ACTUAL_COMMIT" == "$COMMIT" ]] || {
  printf 'Unexpected %s revision: %s\n' "$RELEASE" "$ACTUAL_COMMIT" >&2
  exit 1
}

build_image() {
  image="$1"
  dockerfile="$2"
  target="$3"
  reference="$REGISTRY/$image:$IMAGE_TAG"
  if docker buildx imagetools inspect "$reference" >/dev/null 2>&1; then
    printf 'Already published; leaving immutable tag unchanged: %s\n' "$reference"
    return
  fi
  printf 'Building %s for linux/arm64\n' "$reference"
  if [[ -n "$target" ]]; then
    docker buildx build \
      --platform linux/arm64 \
      --file "$SOURCE/$dockerfile" \
      --target "$target" \
      --label "org.opencontainers.image.revision=$COMMIT" \
      --tag "$reference" \
      --push \
      "$SOURCE"
  else
    docker buildx build \
      --platform linux/arm64 \
      --file "$SOURCE/$dockerfile" \
      --label "org.opencontainers.image.revision=$COMMIT" \
      --tag "$reference" \
      --push \
      "$SOURCE"
  fi
}

build_image code-interpreter-api service/Dockerfile api
build_image code-interpreter-worker service/Dockerfile worker
build_image code-interpreter-sandbox-runner-direct api/Dockerfile sandbox-runner-false
build_image code-interpreter-file-server service/Dockerfile production
build_image code-interpreter-egress-gateway service/Dockerfile.egress-gateway production
build_image code-interpreter-package-init docker/Dockerfile.package-init ""

printf 'CodeAPI %s ARM64 images are ready under %s\n' "$RELEASE" "$REGISTRY"