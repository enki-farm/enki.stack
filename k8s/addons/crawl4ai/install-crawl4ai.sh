#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NAMESPACE="crawl4ai"
ENV_FILE="$SCRIPT_DIR/.env"
SECRET_NAME="crawl4ai-credentials"

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
Install or update Crawl4AI (with the Firecrawl-compatible shim sidecar) in-cluster.

Usage:
  ./k8s/addons/crawl4ai/install-crawl4ai.sh [options]

Options:
  --dry-run                Render the manifests without applying
  -h, --help               Show this help

Secrets:
  Values are read from k8s/addons/crawl4ai/.env (see .env.example). Missing
  values are reused from the existing crawl4ai-credentials Secret, otherwise
  generated once with openssl.

Notes:
  - Internal only: Service crawl4ai.crawl4ai.svc.cluster.local, ports 11235
    (Crawl4AI API) and 3002 (Firecrawl shim). No HTTPRoute.
  - Build and push ghcr.io/enki-farm/firecrawl-shim first (docker/firecrawl-shim).
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

secret_value() {
  local key="$1" value
  value="$(env_file_value "$key")"
  [[ -n "$value" ]] || value="$(existing_secret_value "$key")"
  if [[ -z "$value" ]]; then
    log "Generating $key" >&2
    value="$(openssl rand -hex 32)"
  fi
  printf '%s' "$value"
}

ensure_namespace() {
  log "Ensuring namespace exists: $NAMESPACE"
  kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}

ensure_secret() {
  local gateway_key
  gateway_key="$(env_file_value ENKI_GATEWAY_API_KEY)"
  [[ -n "$gateway_key" ]] || gateway_key="$(existing_secret_value OPENAI_API_KEY)"
  if [[ -z "$gateway_key" ]]; then
    warn "ENKI_GATEWAY_API_KEY not set; using placeholder (gateway has no client auth yet)"
    gateway_key="sk-local-vllm"
  fi

  log "Applying Secret $NAMESPACE/$SECRET_NAME"
  kubectl -n "$NAMESPACE" create secret generic "$SECRET_NAME" \
    --from-literal="CRAWL4AI_API_TOKEN=$(secret_value CRAWL4AI_API_TOKEN)" \
    --from-literal="FIRECRAWL_SHIM_API_KEY=$(secret_value FIRECRAWL_SHIM_API_KEY)" \
    --from-literal="OPENAI_API_KEY=$gateway_key" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}

main() {
  parse_args "$@"
  require_bin kubectl

  if [[ "$DRY_RUN" == "true" ]]; then
    printf '[DRY-RUN] kubectl create namespace %s\n' "$NAMESPACE"
    printf '[DRY-RUN] kubectl -n %s create secret generic %s ...\n' "$NAMESPACE" "$SECRET_NAME"
    kubectl kustomize "$SCRIPT_DIR"
    return 0
  fi

  require_bin openssl

  ensure_namespace
  ensure_secret

  log "Applying Crawl4AI manifests"
  kubectl apply -k "$SCRIPT_DIR"
  kubectl -n "$NAMESPACE" rollout status deployment/crawl4ai --timeout=5m
}

main "$@"
