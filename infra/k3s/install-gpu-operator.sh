#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VALUES_FILE="$ROOT_DIR/infra/k3s/values-gpu-operator.yaml"

NAMESPACE="gpu-operator"
RELEASE_NAME="gpu-operator"
CHART_VERSION=""

DRY_RUN="false"

log() {
  printf '[INFO] %s\n' "$*"
}

err() {
  printf '[ERROR] %s\n' "$*" >&2
}

usage() {
  cat <<'EOF'
Install the NVIDIA GPU Operator on the DGX Spark k3s cluster.

Usage:
  ./infra/k3s/install-gpu-operator.sh [options]

Options:
  --values <path>       Helm values file (default: infra/k3s/values-gpu-operator.yaml)
  --chart-version <ver> Pin the gpu-operator chart version (default: latest)
  --namespace <name>    Namespace for the operator (default: gpu-operator)
  --dry-run             Print commands without executing
  -h, --help            Show this help

Notes:
  - Assumes DGX OS's pre-installed NVIDIA driver; values file sets
    driver.enabled=false so the operator only manages toolkit/device-plugin/DCGM.
  - Requires helm and an existing kubeconfig (run infra/k3s/install-k3s.sh first).

Examples:
  ./infra/k3s/install-gpu-operator.sh
  ./infra/k3s/install-gpu-operator.sh --dry-run
EOF
}

require_bin() {
  local bin="$1"
  if ! command -v "$bin" >/dev/null 2>&1; then
    err "Required binary not found: $bin"
    exit 1
  fi
}

run_cmd() {
  if [[ "$DRY_RUN" == "true" ]]; then
    printf '[DRY-RUN] %s\n' "$*"
    return 0
  fi
  "$@"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --values)
        VALUES_FILE="$2"
        shift 2
        ;;
      --chart-version)
        CHART_VERSION="$2"
        shift 2
        ;;
      --namespace)
        NAMESPACE="$2"
        shift 2
        ;;
      --dry-run)
        DRY_RUN="true"
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        err "Unknown argument: $1"
        usage
        exit 1
        ;;
    esac
  done
}

validate_prereqs() {
  require_bin helm
  require_bin kubectl
  if [[ ! -f "$VALUES_FILE" ]]; then
    err "Values file not found: $VALUES_FILE"
    exit 1
  fi
}

install_gpu_operator() {
  log "Adding/updating the nvidia Helm repo"
  run_cmd helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
  run_cmd helm repo update

  local helm_args=(upgrade --install "$RELEASE_NAME" nvidia/gpu-operator
    --namespace "$NAMESPACE" --create-namespace
    -f "$VALUES_FILE" --wait)
  if [[ -n "$CHART_VERSION" ]]; then
    helm_args+=(--version "$CHART_VERSION")
  fi

  log "Installing GPU Operator (namespace: $NAMESPACE)"
  run_cmd helm "${helm_args[@]}"
}

wait_for_device_plugin() {
  log "Waiting for the nvidia-device-plugin daemonset rollout"
  run_cmd kubectl -n "$NAMESPACE" rollout status daemonset/nvidia-device-plugin-daemonset --timeout=10m
}

main() {
  parse_args "$@"
  validate_prereqs
  install_gpu_operator
  if [[ "$DRY_RUN" != "true" ]]; then
    wait_for_device_plugin
  fi
  log "GPU Operator install complete"
}

main "$@"
