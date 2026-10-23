#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMMIT=0147e443973aed81d2746102d3f0e6205890fa59
SHA256=c7991da5a69a4f565cb4b418494513bed1e265c761482e00a80b08da4caeb28b
DESTINATION="$ROOT_DIR/k8s/kubeflow-hub/upstream"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT
[[ ! -e "$DESTINATION" ]] || { printf 'Refusing to overwrite %s\n' "$DESTINATION" >&2; exit 1; }
curl --fail --location --silent --show-error "https://codeload.github.com/kubeflow/hub/tar.gz/$COMMIT" -o "$TEMP_DIR/hub.tar.gz"
[[ "$(shasum -a 256 "$TEMP_DIR/hub.tar.gz" | awk '{print $1}')" == "$SHA256" ]] || { printf 'Archive checksum mismatch\n' >&2; exit 1; }
mkdir -p "$DESTINATION"
tar -xzf "$TEMP_DIR/hub.tar.gz" -C "$DESTINATION" --strip-components=3 "hub-$COMMIT/manifests/kustomize"
tar -xzf "$TEMP_DIR/hub.tar.gz" -C "$DESTINATION" --strip-components=1 "hub-$COMMIT/LICENSE"
printf 'Vendored Hub v0.3.17 (%s)\n' "$COMMIT"