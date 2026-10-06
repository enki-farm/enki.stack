#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NAMESPACE="librechat"
RELEASE_NAME="librechat"
CHART="oci://ghcr.io/librechat-ai/librechat-chart/librechat"
CHART_VERSION="2.0.13"
VALUES_FILE="$SCRIPT_DIR/values.yaml"
ENV_FILE="$SCRIPT_DIR/.env"
ROUTE_FILE="$SCRIPT_DIR/httproute.yaml"
SECRET_NAME="librechat-credentials-env"
SECRET_KEYS=(ENKI_GATEWAY_API_KEY CREDS_KEY CREDS_IV JWT_SECRET JWT_REFRESH_SECRET MEILI_MASTER_KEY)

DRY_RUN="false"

log() {
  printf '[INFO] %s\n' "$*"
}

warn() {
  printf '[WARN] %s\n' "$*"
}

err() {
  printf '[ERROR] %s\n' "$*" >&2
}

usage() {
  cat <<'EOF'
Install or update LibreChat via its official Helm chart, wired to the enki AI Gateway.

Usage:
  ./k8s/addons/librechat/install-librechat.sh [options]

Options:
  --namespace <name>       Target namespace (default: librechat)
  --chart-version <ver>    Chart version (default: pinned in script)
  --dry-run                Render the chart and print commands without applying
  -h, --help               Show this help

Secrets:
  Values are read from k8s/addons/librechat/.env (see .env.example). Missing
  credentials are reused from the existing librechat-credentials-env Secret,
  otherwise generated once with openssl.

Notes:
  - Install the gateway and apply models/default first; LibreChat only exposes
    the "Enki Gateway" custom endpoint (https://ai.zer0.garden/v1).
  - Publishes chat.zer0.garden through the shared Envoy Gateway (httproute.yaml).
EOF
}

require_bin() {
  if ! command -v "$1" >/dev/null 2>&1; then
    err "Required binary not found: $1"
    exit 1
  fi
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --namespace)
        NAMESPACE="$2"
        shift 2
        ;;
      --chart-version)
        CHART_VERSION="$2"
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

# Parsed rather than sourced so .env content is never executed.
env_file_value() {
  local key="$1" line value
  [[ -f "$ENV_FILE" ]] || return 0
  line="$(command grep -E "^[[:space:]]*${key}=" "$ENV_FILE" | tail -n 1 || true)"
  value="${line#*=}"
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

existing_secret_value() {
  local key="$1"
  kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" -o "jsonpath={.data.$key}" 2>/dev/null | base64 --decode 2>/dev/null || true
}

generate_value() {
  case "$1" in
    CREDS_IV) openssl rand -hex 16 ;;
    *) openssl rand -hex 32 ;;
  esac
}

ensure_namespace() {
  log "Ensuring namespace exists: $NAMESPACE"
  kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}

ensure_secret() {
  local key value args=()
  for key in "${SECRET_KEYS[@]}"; do
    value="$(env_file_value "$key")"
    [[ -n "$value" ]] || value="$(existing_secret_value "$key")"
    if [[ -z "$value" ]]; then
      if [[ "$key" == "ENKI_GATEWAY_API_KEY" ]]; then
        warn "ENKI_GATEWAY_API_KEY not set; using placeholder (gateway has no client auth yet)"
        value="sk-local-vllm"
      else
        log "Generating $key"
        value="$(generate_value "$key")"
      fi
    fi
    args+=("--from-literal=$key=$value")
  done

  log "Applying Secret $NAMESPACE/$SECRET_NAME"
  kubectl -n "$NAMESPACE" create secret generic "$SECRET_NAME" "${args[@]}" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}

main() {
  parse_args "$@"
  require_bin helm

  if [[ ! -f "$VALUES_FILE" ]]; then
    err "Values file not found: $VALUES_FILE"
    exit 1
  fi

  if [[ "$DRY_RUN" == "true" ]]; then
    printf '[DRY-RUN] kubectl create namespace %s\n' "$NAMESPACE"
    printf '[DRY-RUN] kubectl -n %s create secret generic %s ...\n' "$NAMESPACE" "$SECRET_NAME"
    helm template "$RELEASE_NAME" "$CHART" --version "$CHART_VERSION" \
      --namespace "$NAMESPACE" -f "$VALUES_FILE"
    printf '[DRY-RUN] kubectl apply -f %s\n' "$ROUTE_FILE"
    return 0
  fi

  require_bin kubectl
  require_bin openssl

  ensure_namespace
  ensure_secret

  log "Installing LibreChat chart $CHART_VERSION (namespace: $NAMESPACE)"
  helm upgrade --install "$RELEASE_NAME" "$CHART" \
    --version "$CHART_VERSION" \
    --namespace "$NAMESPACE" \
    -f "$VALUES_FILE" \
    --wait --timeout 10m

  log "Applying HTTPRoute (chat.zer0.garden)"
  kubectl -n "$NAMESPACE" apply -f "$ROUTE_FILE"
}

main "$@"
