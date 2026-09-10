#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="envoy-gateway-system"
RELEASE_NAME="eg"
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
Install the Envoy Gateway controller (implements the Gateway API GatewayClass
referenced by k8s/gateway-api/gatewayclass.yaml).

Usage:
  ./infra/gateway/install-envoy-gateway.sh [options]

Options:
  --chart-version <ver> Pin the envoy-gateway chart version (default: latest)
  --namespace <name>    Namespace for the controller (default: envoy-gateway-system)
  --dry-run             Print commands without executing
  -h, --help             Show this help

Notes:
  - Apply the Gateway API CRDs first: kubectl apply --server-side -f k8s/gateway-api/deployment.yaml
  - Run before applying k8s/gateway-api/gatewayclass.yaml and gateway.yaml.
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

main() {
  parse_args "$@"
  if [[ "$DRY_RUN" != "true" ]]; then
    require_bin helm
    require_bin kubectl
  fi

  local helm_args=(upgrade --install "$RELEASE_NAME"
    oci://docker.io/envoyproxy/gateway-helm
    --namespace "$NAMESPACE" --create-namespace --wait)
  if [[ -n "$CHART_VERSION" ]]; then
    helm_args+=(--version "$CHART_VERSION")
  fi

  log "Installing Envoy Gateway (namespace: $NAMESPACE)"
  run_cmd helm "${helm_args[@]}"

  log "Waiting for the Envoy Gateway controller rollout"
  run_cmd kubectl -n "$NAMESPACE" rollout status deploy/envoy-gateway --timeout=5m
}

main "$@"
