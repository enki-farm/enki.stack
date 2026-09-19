#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NAMESPACE="kserve"
CHART_VERSION="v0.20.0"

KSERVE_CRD_RELEASE_NAME="kserve-crd"
KSERVE_RELEASE_NAME="kserve-resources"
LLMISVC_CRD_RELEASE_NAME="kserve-llmisvc-crd"
LLMISVC_RELEASE_NAME="kserve-llmisvc-resources"
RUNTIME_CONFIGS_RELEASE_NAME="kserve-runtime-configs"

KSERVE_VALUES_FILE="$SCRIPT_DIR/values-kserve-resources.yaml"
LLMISVC_VALUES_FILE="$SCRIPT_DIR/values-kserve-llmisvc-resources.yaml"
RUNTIME_CONFIGS_VALUES_FILE="$SCRIPT_DIR/values-kserve-runtime-configs.yaml"

DRY_RUN="false"

log() {
  printf '[INFO] %s\n' "$*"
}

err() {
  printf '[ERROR] %s\n' "$*" >&2
}

usage() {
  cat <<'EOF'
Install or update KServe and LLMInferenceService with Helm.

Usage:
  ./infra/kserve/install-kserve.sh [options]

Options:
  --chart-version <ver>  Pin the KServe chart version (default: v0.20.0)
  --namespace <name>     Namespace for KServe releases (default: kserve)
  --dry-run              Print Helm commands without executing
  -h, --help             Show this help

Notes:
  - The values files are full local copies of the upstream chart defaults with
    enki.stack overrides applied.
  - KServe ingress creation is disabled because external traffic is routed
    through Envoy AI Gateway to cluster-local predictor services.
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
  if [[ "$DRY_RUN" != "true" ]]; then
    require_bin helm
    require_bin kubectl
  fi

  local file
  for file in "$KSERVE_VALUES_FILE" "$LLMISVC_VALUES_FILE" "$RUNTIME_CONFIGS_VALUES_FILE"; do
    if [[ ! -f "$file" ]]; then
      err "Values file not found: $file"
      exit 1
    fi
  done
}

install_kserve() {
  log "Installing KServe CRDs (namespace: $NAMESPACE)"
  run_cmd helm upgrade --install "$KSERVE_CRD_RELEASE_NAME" oci://ghcr.io/kserve/charts/kserve-crd \
    --version "$CHART_VERSION" \
    --namespace "$NAMESPACE" --create-namespace --wait

  log "Installing KServe resources (namespace: $NAMESPACE)"
  run_cmd helm upgrade --install "$KSERVE_RELEASE_NAME" oci://ghcr.io/kserve/charts/kserve-resources \
    --version "$CHART_VERSION" \
    --namespace "$NAMESPACE" --create-namespace \
    -f "$KSERVE_VALUES_FILE" \
    --wait --timeout 10m
}

install_llmisvc() {
  log "Installing LLMInferenceService CRDs (namespace: $NAMESPACE)"
  run_cmd helm upgrade --install "$LLMISVC_CRD_RELEASE_NAME" oci://ghcr.io/kserve/charts/kserve-llmisvc-crd \
    --version "$CHART_VERSION" \
    --namespace "$NAMESPACE" --create-namespace --wait

  log "Installing LLMInferenceService resources (namespace: $NAMESPACE)"
  run_cmd helm upgrade --install "$LLMISVC_RELEASE_NAME" oci://ghcr.io/kserve/charts/kserve-llmisvc-resources \
    --version "$CHART_VERSION" \
    --namespace "$NAMESPACE" --create-namespace \
    -f "$LLMISVC_VALUES_FILE" \
    --wait --timeout 10m

  log "Installing KServe runtime configs (namespace: $NAMESPACE)"
  run_cmd helm upgrade --install "$RUNTIME_CONFIGS_RELEASE_NAME" oci://ghcr.io/kserve/charts/kserve-runtime-configs \
    --version "$CHART_VERSION" \
    --namespace "$NAMESPACE" --create-namespace \
    -f "$RUNTIME_CONFIGS_VALUES_FILE" \
    --wait --timeout 10m
}

main() {
  parse_args "$@"
  validate_prereqs
  install_kserve
  install_llmisvc
}

main "$@"