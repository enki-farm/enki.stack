#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NAMESPACE="librechat"
RELEASE_NAME="librechat"
CHART="oci://ghcr.io/librechat-ai/librechat-chart/librechat"
CHART_VERSION="2.0.17"
VALUES_FILE="$SCRIPT_DIR/values.yaml"
ENV_FILE="$SCRIPT_DIR/.env"
ROUTE_FILE="$SCRIPT_DIR/httproute.yaml"
SECRET_NAME="librechat-credentials-env"
SECRET_KEYS=(ENKI_GATEWAY_API_KEY CREDS_KEY CREDS_IV JWT_SECRET JWT_REFRESH_SECRET MEILI_MASTER_KEY FIRECRAWL_API_KEY)
CRAWL4AI_NAMESPACE="crawl4ai"
CRAWL4AI_SECRET_NAME="crawl4ai-credentials"

DRY_RUN="false"
AUTH_DIR=""

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
  --auth-dir <directory>   Opt-in Dex overrides from scripts/install-auth.sh
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
  - Web search uses the internal searxng and crawl4ai add-ons; install them first.
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
      --auth-dir)
        [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || { err '--auth-dir requires a directory'; exit 1; }
        AUTH_DIR="$2"
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

crawl4ai_shim_key() {
  kubectl -n "$CRAWL4AI_NAMESPACE" get secret "$CRAWL4AI_SECRET_NAME" -o "jsonpath={.data.FIRECRAWL_SHIM_API_KEY}" 2>/dev/null | base64 --decode 2>/dev/null || true
}

crawl4ai_api_token() {
  kubectl -n "$CRAWL4AI_NAMESPACE" get secret "$CRAWL4AI_SECRET_NAME" -o "jsonpath={.data.CRAWL4AI_API_TOKEN}" 2>/dev/null | base64 --decode 2>/dev/null || true
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
  local key value crawl_token args=()
  for key in "${SECRET_KEYS[@]}"; do
    value="$(env_file_value "$key")"
    # Must equal the shim key; prefer the live crawl4ai Secret so the two never drift.
    if [[ -z "$value" && "$key" == "FIRECRAWL_API_KEY" ]]; then
      value="$(crawl4ai_shim_key)"
    fi
    [[ -n "$value" ]] || value="$(existing_secret_value "$key")"
    if [[ -z "$value" ]]; then
      if [[ "$key" == "ENKI_GATEWAY_API_KEY" ]]; then
        warn "ENKI_GATEWAY_API_KEY not set; using placeholder (gateway has no client auth yet)"
        value="sk-local-vllm"
      elif [[ "$key" == "FIRECRAWL_API_KEY" ]]; then
        warn "FIRECRAWL_API_KEY not found; install crawl4ai first or web scraping will fail"
        continue
      else
        log "Generating $key"
        value="$(generate_value "$key")"
      fi
    fi
    args+=("--from-literal=$key=$value")
  done

  crawl_token="$(crawl4ai_api_token)"
  if [[ -z "$crawl_token" ]]; then
    err "CRAWL4AI_API_TOKEN not found in $CRAWL4AI_NAMESPACE/$CRAWL4AI_SECRET_NAME; install Crawl4AI first"
    return 1
  fi
  args+=("--from-literal=CRAWL4AI_API_TOKEN=$crawl_token")

  log "Applying Secret $NAMESPACE/$SECRET_NAME"
  kubectl -n "$NAMESPACE" create secret generic "$SECRET_NAME" "${args[@]}" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}

main() {
  parse_args "$@"
  require_bin helm
  local auth_args=()
  if [[ -n "$AUTH_DIR" ]]; then
    [[ "$NAMESPACE" == librechat ]] || { err 'Dex overrides require namespace librechat'; exit 1; }
    [[ -f "$AUTH_DIR/librechat-values.yaml" && -f "$AUTH_DIR/librechat-route.yaml" ]] || {
      err 'Auth overrides not found; run scripts/install-auth.sh first'
      exit 1
    }
    auth_args=(-f "$AUTH_DIR/librechat-values.yaml")
    ROUTE_FILE="$AUTH_DIR/librechat-route.yaml"
  fi

  if [[ ! -f "$VALUES_FILE" ]]; then
    err "Values file not found: $VALUES_FILE"
    exit 1
  fi

  if [[ "$DRY_RUN" == "true" ]]; then
    printf '[DRY-RUN] kubectl create namespace %s\n' "$NAMESPACE"
    printf '[DRY-RUN] kubectl -n %s create secret generic %s ...\n' "$NAMESPACE" "$SECRET_NAME"
    helm template "$RELEASE_NAME" "$CHART" --version "$CHART_VERSION" \
      --namespace "$NAMESPACE" -f "$VALUES_FILE" ${auth_args[@]+"${auth_args[@]}"}
    printf '[DRY-RUN] kubectl apply -f %s\n' "$ROUTE_FILE"
    return 0
  fi

  require_bin kubectl
  require_bin openssl
  if [[ -z "$AUTH_DIR" ]]; then
    local existing_auth
    existing_auth="$(kubectl -n "$NAMESPACE" get secret oidc-credentials --ignore-not-found -o jsonpath='{.metadata.name}')"
    [[ -z "$existing_auth" ]] || {
      err 'OIDC credentials already exist; --auth-dir is required to avoid restoring legacy login'
      exit 1
    }
  fi
  if [[ -n "$AUTH_DIR" ]]; then
    kubectl -n "$NAMESPACE" get secret oidc-credentials >/dev/null || {
      err 'Missing oidc-credentials; run scripts/install-auth.sh without --dry-run first'
      exit 1
    }
  fi

  ensure_namespace
  ensure_secret

  log "Installing LibreChat chart $CHART_VERSION (namespace: $NAMESPACE)"
  helm upgrade --install "$RELEASE_NAME" "$CHART" \
    --version "$CHART_VERSION" \
    --namespace "$NAMESPACE" \
    -f "$VALUES_FILE" \
    ${auth_args[@]+"${auth_args[@]}"} \
    --wait --timeout 10m

  log "Applying HTTPRoute: $ROUTE_FILE"
  kubectl -n "$NAMESPACE" apply -f "$ROUTE_FILE"
}

main "$@"
