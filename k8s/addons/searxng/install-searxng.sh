#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NAMESPACE="searxng"
ENV_FILE="$SCRIPT_DIR/.env"
SECRET_NAME="searxng-credentials"

DRY_RUN="false"

log() {
  printf '[INFO] %s\n' "$*"
}

err() {
  printf '[ERROR] %s\n' "$*" >&2
}

usage() {
  cat <<'EOF'
Install or update the internal SearXNG instance used by LibreChat web search.

Usage:
  ./k8s/addons/searxng/install-searxng.sh [options]

Options:
  --dry-run                Render the manifests without applying
  -h, --help               Show this help

Secrets:
  SEARXNG_SECRET is read from k8s/addons/searxng/.env (see .env.example),
  reused from the existing searxng-credentials Secret, or generated once.

Notes:
  - Internal only: http://searxng.searxng.svc.cluster.local:8080. No HTTPRoute.
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

ensure_secret() {
  local value
  value="$(env_file_value SEARXNG_SECRET)"
  [[ -n "$value" ]] || value="$(existing_secret_value SEARXNG_SECRET)"
  if [[ -z "$value" ]]; then
    log "Generating SEARXNG_SECRET"
    value="$(openssl rand -hex 32)"
  fi

  log "Applying Secret $NAMESPACE/$SECRET_NAME"
  kubectl -n "$NAMESPACE" create secret generic "$SECRET_NAME" \
    --from-literal="SEARXNG_SECRET=$value" \
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

  log "Ensuring namespace exists: $NAMESPACE"
  kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  ensure_secret

  log "Applying SearXNG manifests"
  kubectl apply -k "$SCRIPT_DIR"
  kubectl -n "$NAMESPACE" rollout status deployment/searxng --timeout=5m
}

main "$@"
