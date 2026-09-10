#!/usr/bin/env bash
set -euo pipefail

# Envoy AI Gateway was renamed to "Agent Router" upstream (same CRDs/API group
# `aigateway.envoyproxy.io`, same namespace/chart names) — see
# https://github.com/envoyproxy/ai-gateway (redirects to theagentrouter/agent-router).

NAMESPACE="envoy-ai-gateway-system"
RELEASE_NAME="aieg"
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
Install the Envoy AI Gateway (aka Agent Router) CRDs + controller on top of
an existing Envoy Gateway install.

Usage:
  ./infra/gateway/install-ai-gateway.sh [options]

Options:
  --chart-version <ver> Pin the ai-gateway-helm chart version (default: latest)
  --namespace <name>    Namespace for the controller (default: envoy-ai-gateway-system)
  --dry-run             Print commands without executing
  -h, --help             Show this help

Notes:
  - Requires infra/gateway/install-envoy-gateway.sh to have run first.
  - After this, apply k8s/addons/envoy-ai-gateway (AIGatewayRoute/AIServiceBackend CRs).
  - Verify the exact chart name/version against current upstream docs
    (https://theagentrouter.ai/docs/getting-started/) before pinning in CI.
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
  require_bin helm
  require_bin kubectl

  local helm_args=(upgrade --install "$RELEASE_NAME"
    oci://docker.io/envoyproxy/ai-gateway-helm
    --namespace "$NAMESPACE" --create-namespace --wait)
  if [[ -n "$CHART_VERSION" ]]; then
    helm_args+=(--version "$CHART_VERSION")
  fi

  log "Installing Envoy AI Gateway / Agent Router (namespace: $NAMESPACE)"
  run_cmd helm "${helm_args[@]}"

  log "Waiting for the AI Gateway controller rollout"
  run_cmd kubectl -n "$NAMESPACE" rollout status deploy/ai-gateway-controller --timeout=5m || {
    err "Controller deployment name may differ by chart version; inspect with: kubectl -n $NAMESPACE get deploy"
  }
}

main "$@"
