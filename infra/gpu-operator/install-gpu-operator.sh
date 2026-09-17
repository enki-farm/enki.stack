#!/usr/bin/env bash
set -euo pipefail

# Install the NVIDIA GPU Operator on the DGX Spark k3s cluster.
# Assumes DGX OS's pre-installed NVIDIA driver; values file sets
# driver.enabled=false so the operator only manages toolkit/device-plugin/DCGM.
# Requires helm and an existing kubeconfig (run infra/k3s/install-k3s.sh first).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NAMESPACE="gpu-operator"
RELEASE_NAME="gpu-operator"
CHART_VERSION=""
VALUES_FILE="$SCRIPT_DIR/values-gpu-operator.yaml"

log() {
  printf '[INFO] %s\n' "$*"
}

err() {
  printf '[ERROR] %s\n' "$*" >&2
}

require_bin() {
  local bin="$1"
  if ! command -v "$bin" >/dev/null 2>&1; then
    err "Required binary not found: $bin"
    exit 1
  fi
}

main() {
  require_bin helm
  require_bin kubectl
  if [[ ! -f "$VALUES_FILE" ]]; then
    err "Values file not found: $VALUES_FILE"
    exit 1
  fi

  log "Adding/updating the nvidia Helm repo"
  helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
  helm repo update

  local helm_args=(upgrade --install "$RELEASE_NAME" nvidia/gpu-operator
    --namespace "$NAMESPACE" --create-namespace
    -f "$VALUES_FILE" --wait)
  if [[ -n "$CHART_VERSION" ]]; then
    helm_args+=(--version "$CHART_VERSION")
  fi

  log "Installing GPU Operator (namespace: $NAMESPACE)"
  helm "${helm_args[@]}"

  log "Waiting for the nvidia-device-plugin daemonset rollout"
  kubectl -n "$NAMESPACE" rollout status daemonset/nvidia-device-plugin-daemonset --timeout=10m

  log "GPU Operator install complete"
}

main "$@"
