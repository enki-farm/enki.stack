#!/usr/bin/env bash
set -euo pipefail

RELEASE=warpgate-operator-v0.5.0
COMMIT=773919e6de7f7f6f4968bc75423a0076b7225736
REGISTRY=ghcr.io/enki-farm
IMAGE=warpgate-operator
IMAGE_TAG="${IMAGE_TAG:-v0.5.0-enki2}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

command -v git >/dev/null || { printf 'git is required\n' >&2; exit 1; }
command -v docker >/dev/null || { printf 'docker is required\n' >&2; exit 1; }
docker buildx version >/dev/null

SOURCE="$TEMP_DIR/warpgate-operator"
git clone --quiet --depth 1 --branch "$RELEASE" \
  https://github.com/thereisnotime/warpgate-operator.git "$SOURCE"
ACTUAL_COMMIT="$(git -C "$SOURCE" rev-parse HEAD)"
[[ "$ACTUAL_COMMIT" == "$COMMIT" ]] || {
  printf 'Unexpected %s revision: %s\n' "$RELEASE" "$ACTUAL_COMMIT" >&2
  exit 1
}

SOURCE_FILE="$SOURCE/internal/controller/warpgateinstance_controller.go"
OLD_ARGS='"run", "--skip-securing-files"'
NEW_ARGS='"--skip-securing-files", "run"'
[[ "$(rg -F -c "$OLD_ARGS" "$SOURCE_FILE")" == "1" ]] || {
  printf 'Expected one Warpgate run-argument sequence in %s\n' "$SOURCE_FILE" >&2
  exit 1
}
perl -0pi -e 's/"run", "--skip-securing-files"/"--skip-securing-files", "run"/' \
  "$SOURCE_FILE"

OLD_ROLLOUT_CHECK='existingImage != desiredImage || existingHash != desiredHash || *existing.Spec.Replicas != *desired.Spec.Replicas'
[[ "$(rg -F -c "$OLD_ROLLOUT_CHECK" "$SOURCE_FILE")" == "1" ]] || {
  printf 'Expected one Deployment rollout comparison in %s\n' "$SOURCE_FILE" >&2
  exit 1
}
perl -0pi -e 's/\QexistingImage != desiredImage || existingHash != desiredHash || *existing.Spec.Replicas != *desired.Spec.Replicas\E/existingImage != desiredImage || existingHash != desiredHash || !equality.Semantic.DeepEqual(existing.Spec.Template.Spec.Containers[0].Args, desired.Spec.Template.Spec.Containers[0].Args) || *existing.Spec.Replicas != *desired.Spec.Replicas/' \
  "$SOURCE_FILE"

REFERENCE="$REGISTRY/$IMAGE:$IMAGE_TAG"
if docker buildx imagetools inspect "$REFERENCE" >/dev/null 2>&1; then
  printf 'Already published; leaving immutable tag unchanged: %s\n' "$REFERENCE"
  exit 0
fi

docker buildx build \
  --platform "$PLATFORMS" \
  --file "$SOURCE/Containerfile" \
  --label "org.opencontainers.image.revision=$COMMIT" \
  --label "org.opencontainers.image.version=$IMAGE_TAG" \
  --tag "$REFERENCE" \
  --push \
  "$SOURCE"

printf 'Published %s\n' "$REFERENCE"