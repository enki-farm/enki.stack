#!/usr/bin/env bash
set -euo pipefail

# Envoy AI Gateway was renamed to "Agent Router" upstream (same CRDs/API group
# `aigateway.envoyproxy.io`, same namespace/chart names) — see
# https://github.com/envoyproxy/ai-gateway (redirects to theagentrouter/agent-router).
#
# Installs Envoy Gateway (with the AI Gateway integration values) followed by
# the Agent Router CRDs + controller. Re-check versions against
# https://theagentrouter.ai/docs/getting-started/ before bumping these.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

EG_NAMESPACE="envoy-gateway-system"
EG_RELEASE_NAME="eg"
EG_CHART_VERSION="v1.8.1"
EG_VALUES_FILE="$SCRIPT_DIR/envoy-gateway-values.yaml"

AIGW_NAMESPACE="envoy-ai-gateway-system"
AIGW_CRD_RELEASE_NAME="aieg-crd"
AIGW_RELEASE_NAME="aieg"
AIGW_CHART_VERSION="v1.1.0"
AIGW_VALUES_FILE="$SCRIPT_DIR/ai-gateway-values.yaml"

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

  log "Installing Envoy Gateway (namespace: $EG_NAMESPACE)"
  helm upgrade --install "$EG_RELEASE_NAME" oci://docker.io/envoyproxy/gateway-helm \
    --version "$EG_CHART_VERSION" \
    --namespace "$EG_NAMESPACE" --create-namespace --wait \
    -f "$EG_VALUES_FILE"
  kubectl wait --timeout=2m -n "$EG_NAMESPACE" deployment/envoy-gateway --for=condition=Available

  log "Installing Envoy AI Gateway / Agent Router CRDs (namespace: $AIGW_NAMESPACE)"
  helm upgrade --install "$AIGW_CRD_RELEASE_NAME" oci://docker.io/envoyproxy/ai-gateway-crds-helm \
    --version "$AIGW_CHART_VERSION" \
    --namespace "$AIGW_NAMESPACE" --create-namespace --wait

  log "Installing Envoy AI Gateway / Agent Router controller (namespace: $AIGW_NAMESPACE)"
  helm upgrade --install "$AIGW_RELEASE_NAME" oci://docker.io/envoyproxy/ai-gateway-helm \
    --version "$AIGW_CHART_VERSION" \
    --namespace "$AIGW_NAMESPACE" --create-namespace \
    -f "$AIGW_VALUES_FILE"
  kubectl wait --timeout=2m -n "$AIGW_NAMESPACE" deployment/ai-gateway-controller --for=condition=Available
}

main "$@"
