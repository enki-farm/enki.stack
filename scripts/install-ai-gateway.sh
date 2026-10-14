#!/usr/bin/env bash
set -euo pipefail

# Envoy AI Gateway was renamed to "Agent Router" upstream (same CRDs/API group
# `aigateway.envoyproxy.io`, same namespace/chart names) — see
# https://github.com/envoyproxy/ai-gateway (redirects to theagentrouter/agent-router).
#
# Installs Envoy Gateway (with the AI Gateway integration values) followed by
# the Agent Router CRDs + controller. Re-check versions against
# https://theagentrouter.ai/docs/getting-started/ before bumping these.
#
# The extProc's OTLP exporter points at Tempo in the observability namespace.
# Core deployment installs monitoring after the gateway and Dex are available.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

EG_NAMESPACE="envoy-gateway-system"
EG_RELEASE_NAME="eg"
EG_CHART_VERSION="v1.9.2"
EG_VALUES_FILE="$ROOT_DIR/helm/gateway/envoy-gateway-values.yaml"

AIGW_NAMESPACE="envoy-ai-gateway-system"
AIGW_CRD_RELEASE_NAME="aieg-crd"
AIGW_RELEASE_NAME="aieg"
AIGW_CHART_VERSION="v1.2.0"
AIGW_VALUES_FILE="$ROOT_DIR/helm/gateway/ai-gateway-values.yaml"
ENABLE_RATE_LIMITING=false
CONTEXT=""
TAKE_CRD_OWNERSHIP=false

usage() {
  printf 'Usage: bash scripts/install-ai-gateway.sh [--context NAME] [--enable-usage-rate-limiting] [--take-crd-ownership]\n'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --context)
      [[ $# -ge 2 && -n "$2" && "$2" != -* ]] || { usage >&2; exit 1; }
      CONTEXT="$2"
      shift 2
      ;;
    --enable-usage-rate-limiting) ENABLE_RATE_LIMITING=true; shift ;;
    --take-crd-ownership) TAKE_CRD_OWNERSHIP=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 1 ;;
  esac
done

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
  local kubectl_args=() helm_args=() rate_limit_values=() ownership_args=()
  if [[ -n "$CONTEXT" ]]; then
    kubectl_args=(--context "$CONTEXT")
    helm_args=(--kube-context "$CONTEXT")
  fi
  if [[ "$TAKE_CRD_OWNERSHIP" == true ]]; then
    ownership_args=(--force-conflicts)
  fi

  if [[ "$ENABLE_RATE_LIMITING" == false ]] && [[ -n "$(kubectl ${kubectl_args[@]+"${kubectl_args[@]}"} -n ai-gateway-ratelimit get service redis --ignore-not-found -o name)" ]]; then
    ENABLE_RATE_LIMITING=true
    log "Preserving the installed usage rate limiter"
  fi
  if [[ "$ENABLE_RATE_LIMITING" == true ]]; then
    kubectl ${kubectl_args[@]+"${kubectl_args[@]}"} get storageclass local-path >/dev/null
    rate_limit_values=(-f "$ROOT_DIR/helm/gateway/envoy-gateway-rate-limit-values.yaml")
  fi

  log "Updating Gateway API and Envoy Gateway CRDs"
  kubectl ${kubectl_args[@]+"${kubectl_args[@]}"} apply --server-side --field-manager=enki-gateway-crds \
    ${ownership_args[@]+"${ownership_args[@]}"} -f "$ROOT_DIR/k8s/gateway-api/deployment.yaml"
  helm template eg-crds oci://docker.io/envoyproxy/gateway-crds-helm \
    --version "$EG_CHART_VERSION" --set crds.gatewayAPI.enabled=false --set crds.envoyGateway.enabled=true |
    kubectl ${kubectl_args[@]+"${kubectl_args[@]}"} apply --server-side --field-manager=enki-gateway-crds \
      ${ownership_args[@]+"${ownership_args[@]}"} -f -
  kubectl ${kubectl_args[@]+"${kubectl_args[@]}"} wait --timeout=2m --for=condition=Established \
    crd/gateways.gateway.networking.k8s.io crd/httproutes.gateway.networking.k8s.io \
    crd/securitypolicies.gateway.envoyproxy.io crd/backendtrafficpolicies.gateway.envoyproxy.io

  if [[ "$ENABLE_RATE_LIMITING" == true ]]; then
    log "Installing persistent Redis for usage rate limiting"
    kubectl ${kubectl_args[@]+"${kubectl_args[@]}"} apply -k "$ROOT_DIR/k8s/addons/ai-gateway-ratelimit"
    kubectl ${kubectl_args[@]+"${kubectl_args[@]}"} -n ai-gateway-ratelimit rollout status statefulset/redis --timeout=5m
  fi

  log "Installing Envoy Gateway (namespace: $EG_NAMESPACE)"
  helm ${helm_args[@]+"${helm_args[@]}"} upgrade --install "$EG_RELEASE_NAME" oci://docker.io/envoyproxy/gateway-helm \
    --version "$EG_CHART_VERSION" \
    --namespace "$EG_NAMESPACE" --create-namespace --wait --timeout=5m \
    --skip-crds \
    -f "$EG_VALUES_FILE" ${rate_limit_values[@]+"${rate_limit_values[@]}"}
  kubectl ${kubectl_args[@]+"${kubectl_args[@]}"} wait --timeout=2m -n "$EG_NAMESPACE" deployment/envoy-gateway --for=condition=Available
  if [[ "$ENABLE_RATE_LIMITING" == true ]]; then
    kubectl ${kubectl_args[@]+"${kubectl_args[@]}"} -n "$EG_NAMESPACE" wait --for=create deployment/envoy-ratelimit --timeout=2m
    kubectl ${kubectl_args[@]+"${kubectl_args[@]}"} -n "$EG_NAMESPACE" rollout status deployment/envoy-ratelimit --timeout=5m
  fi

  log "Installing Envoy AI Gateway / Agent Router CRDs (namespace: $AIGW_NAMESPACE)"
  helm ${helm_args[@]+"${helm_args[@]}"} upgrade --install "$AIGW_CRD_RELEASE_NAME" oci://docker.io/envoyproxy/ai-gateway-crds-helm \
    --version "$AIGW_CHART_VERSION" \
    --namespace "$AIGW_NAMESPACE" --create-namespace --wait --timeout=5m

  log "Installing Envoy AI Gateway / Agent Router controller (namespace: $AIGW_NAMESPACE)"
  helm ${helm_args[@]+"${helm_args[@]}"} upgrade --install "$AIGW_RELEASE_NAME" oci://docker.io/envoyproxy/ai-gateway-helm \
    --version "$AIGW_CHART_VERSION" \
    --namespace "$AIGW_NAMESPACE" --create-namespace --wait --timeout=5m \
    -f "$AIGW_VALUES_FILE"
  kubectl ${kubectl_args[@]+"${kubectl_args[@]}"} -n "$AIGW_NAMESPACE" rollout status deployment/ai-gateway-controller --timeout=5m
}

main "$@"
