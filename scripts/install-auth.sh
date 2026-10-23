#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEX_DIR="$ROOT_DIR/k8s/dex"
CONFIG_FILE="$DEX_DIR/auth.env"
CONNECTOR_FILE="$DEX_DIR/connector.yaml"
OUTPUT_DIR="$ROOT_DIR/.generated/auth"
DRY_RUN="false"

DEX_ISSUER=""
CHAT_URL=""
GRAFANA_URL=""
CHAT_GROUP=""
CHAT_ADMIN_URL=""
CHAT_ADMIN_GROUP=""
CHAT_ADMIN_HOST=""
ADMIN_CALLBACK=""
GRAFANA_VIEWER_GROUP=""
GRAFANA_ADMIN_GROUP=""
HUB_URL=""
HUB_GROUP=""
HUB_CLIENT=""
GATEWAY_NAME="envoy-ai-gateway-basic"
GATEWAY_NAMESPACE="envoy-ai-gateway-system"

fail() {
  printf '[ERROR] %s\n' "$*" >&2
  exit 1
}

usage() {
  printf '%s\n' \
    'Install Dex as the single login for LibreChat and Grafana. Does not enable model authentication.' \
    'Usage: bash scripts/install-auth.sh [--config <auth.env>] [--connector <connector.yaml>] [--output-dir <dir>] [--dry-run]' \
    'Defaults: --config k8s/dex/auth.env, --connector k8s/dex/connector.yaml, --output-dir .generated/auth' \
    'Connector secrets ($NAME values in the connector file) are read from the terminal environment.' \
    'Dry-run renders secret-free configuration without accessing the cluster.'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config|--connector|--output-dir)
      [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || fail "$1 requires a value"
      case "$1" in
        --config) CONFIG_FILE="$2" ;;
        --connector) CONNECTOR_FILE="$2" ;;
        --output-dir) OUTPUT_DIR="$2" ;;
      esac
      shift 2
      ;;
    --dry-run) DRY_RUN="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "Unknown argument: $1" ;;
  esac
done

[[ -f "$CONFIG_FILE" ]] || fail "Config not found: $CONFIG_FILE (copy k8s/dex/auth.env.example)"
[[ -f "$CONNECTOR_FILE" ]] || fail "Connector not found: $CONNECTOR_FILE (copy one of k8s/dex/connectors/*.example.yaml)"
command grep -q '^connectors:' "$CONNECTOR_FILE" || fail "$CONNECTOR_FILE must start a top-level 'connectors:' list"

# Parse KEY=VALUE without executing the file.
while IFS= read -r line || [[ -n "$line" ]]; do
  [[ -z "${line//[[:space:]]/}" || "$line" == \#* ]] && continue
  key="${line%%=*}"
  value="${line#*=}"
  case "$key" in
    DEX_ISSUER|CHAT_URL|GRAFANA_URL|CHAT_GROUP|CHAT_ADMIN_URL|CHAT_ADMIN_GROUP|GRAFANA_VIEWER_GROUP|GRAFANA_ADMIN_GROUP|HUB_URL|HUB_GROUP|GATEWAY_NAME|GATEWAY_NAMESPACE)
      printf -v "$key" '%s' "$value" ;;
    *) fail "Unknown key in $CONFIG_FILE: $key" ;;
  esac
done < "$CONFIG_FILE"

origin_re='^https://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?$'
group_re='^[A-Za-z0-9_.:@/-]+$'
name_re='^[a-z0-9][a-z0-9-]*$'
for var in DEX_ISSUER CHAT_URL GRAFANA_URL; do
  [[ "${!var}" =~ $origin_re ]] || fail "$var must be an HTTPS origin without path, credentials or trailing slash"
done
for var in CHAT_GROUP GRAFANA_VIEWER_GROUP; do
  [[ "${!var}" =~ $group_re ]] || fail "$var is required and may only contain letters, digits and _.:@/-"
done
[[ -z "$GRAFANA_ADMIN_GROUP" || "$GRAFANA_ADMIN_GROUP" =~ $group_re ]] || fail 'GRAFANA_ADMIN_GROUP contains unsupported characters'
if [[ -n "$HUB_URL" || -n "$HUB_GROUP" ]]; then
  [[ "$HUB_URL" =~ $origin_re ]] || fail 'HUB_URL must be an HTTPS origin without path or trailing slash'
  [[ "$HUB_GROUP" =~ $group_re ]] || fail 'HUB_GROUP is required and contains unsupported characters'
  [[ "$HUB_GROUP" == enki-farm:core ]] || fail 'This Hub installation requires HUB_GROUP=enki-farm:core'
  for origin in "$DEX_ISSUER" "$CHAT_URL" "$GRAFANA_URL" "$CHAT_ADMIN_URL"; do
    [[ "$(printf '%s' "$origin" | tr '[:upper:]' '[:lower:]')" != "$(printf '%s' "$HUB_URL" | tr '[:upper:]' '[:lower:]')" ]] || fail 'Hub must use a distinct origin'
  done
  HUB_CLIENT="$(printf '  - id: hub\n    name: Kubeflow Hub\n    secretEnv: HUB_CLIENT_SECRET\n    redirectURIs: [\"%s/oauth2/callback\"]' "$HUB_URL")"
fi
for var in GATEWAY_NAME GATEWAY_NAMESPACE; do
  [[ "${!var}" =~ $name_re ]] || fail "$var must be a Kubernetes name"
done

DEX_HOST="${DEX_ISSUER#https://}"; DEX_HOST="${DEX_HOST%%:*}"
CHAT_HOST="${CHAT_URL#https://}"; CHAT_HOST="${CHAT_HOST%%:*}"
GRAFANA_HOST="${GRAFANA_URL#https://}"; GRAFANA_HOST="${GRAFANA_HOST%%:*}"
[[ "$DEX_HOST" != "$CHAT_HOST" && "$DEX_HOST" != "$GRAFANA_HOST" && "$CHAT_HOST" != "$GRAFANA_HOST" ]] ||
  fail 'Dex, LibreChat and Grafana must use distinct hosts'
if [[ -n "$CHAT_ADMIN_URL" || -n "$CHAT_ADMIN_GROUP" ]]; then
  [[ "$CHAT_ADMIN_URL" =~ $origin_re ]] || fail 'CHAT_ADMIN_URL must be an HTTPS origin without path, credentials or trailing slash'
  [[ "$CHAT_ADMIN_GROUP" =~ $group_re ]] || fail 'CHAT_ADMIN_GROUP is required and contains unsupported characters'
  CHAT_ADMIN_HOST="${CHAT_ADMIN_URL#https://}"; CHAT_ADMIN_HOST="${CHAT_ADMIN_HOST%%:*}"
  [[ "$(printf '%s' "$CHAT_ADMIN_HOST" | tr '[:upper:]' '[:lower:]')" != "$(printf '%s' "$DEX_HOST" | tr '[:upper:]' '[:lower:]')" &&
     "$(printf '%s' "$CHAT_ADMIN_HOST" | tr '[:upper:]' '[:lower:]')" != "$(printf '%s' "$CHAT_HOST" | tr '[:upper:]' '[:lower:]')" &&
     "$(printf '%s' "$CHAT_ADMIN_HOST" | tr '[:upper:]' '[:lower:]')" != "$(printf '%s' "$GRAFANA_HOST" | tr '[:upper:]' '[:lower:]')" ]] || fail 'CHAT_ADMIN_URL must use a distinct host'
  CHAT_ADMIN_HOST="$(printf '%s' "$CHAT_ADMIN_HOST" | tr '[:upper:]' '[:lower:]')"
  ADMIN_CALLBACK=", \"$CHAT_URL/api/admin/oauth/openid/callback\""
fi

GRAFANA_ROLE_PATH="(contains(groups[*], '$GRAFANA_VIEWER_GROUP') && 'Viewer')"
if [[ -n "$GRAFANA_ADMIN_GROUP" ]]; then
  GRAFANA_ROLE_PATH="(contains(groups[*], '$GRAFANA_ADMIN_GROUP') && 'Admin') || $GRAFANA_ROLE_PATH"
fi

render() {
  local role_path
  role_path="$(printf '%s' "$GRAFANA_ROLE_PATH" | sed 's/&/\\\&/g')"
  sed \
    -e "s#@DEX_ISSUER@#$DEX_ISSUER#g" -e "s#@CHAT_URL@#$CHAT_URL#g" -e "s#@GRAFANA_URL@#$GRAFANA_URL#g" \
    -e "s#@DEX_HOST@#$DEX_HOST#g" -e "s#@CHAT_HOST@#$CHAT_HOST#g" -e "s#@GRAFANA_HOST@#$GRAFANA_HOST#g" \
    -e "s#@CHAT_GROUP@#$CHAT_GROUP#g" -e "s#@GRAFANA_ROLE_PATH@#$role_path#g" \
    -e "s#@CHAT_ADMIN_URL@#$CHAT_ADMIN_URL#g" -e "s#@CHAT_ADMIN_GROUP@#$CHAT_ADMIN_GROUP#g" \
    -e "s#@CHAT_ADMIN_HOST@#$CHAT_ADMIN_HOST#g" -e "s#@ADMIN_CALLBACK@#$ADMIN_CALLBACK#g" \
    -e "s#@GATEWAY_NAME@#$GATEWAY_NAME#g" -e "s#@GATEWAY_NAMESPACE@#$GATEWAY_NAMESPACE#g" "$@"
}

umask 077
mkdir -p "$OUTPUT_DIR"
for name in librechat-values grafana-values dex-route librechat-route grafana-route; do
  render "$DEX_DIR/templates/$name.yaml" > "$OUTPUT_DIR/$name.yaml"
done
render "$DEX_DIR/config.base.yaml" "$CONNECTOR_FILE" > "$OUTPUT_DIR/dex-config.yaml"
if [[ -n "$HUB_CLIENT" ]]; then
  render "$DEX_DIR/config.base.yaml" > "$OUTPUT_DIR/dex-config.yaml"
  printf '%s\n' "$HUB_CLIENT" >> "$OUTPUT_DIR/dex-config.yaml"
  render "$CONNECTOR_FILE" >> "$OUTPUT_DIR/dex-config.yaml"
  printf 'HUB_URL=%s\nHUB_GROUP=%s\nDEX_ISSUER=%s\n' "$HUB_URL" "$HUB_GROUP" "$DEX_ISSUER" > "$OUTPUT_DIR/hub.env"
  render "$DEX_DIR/templates/hub-route.yaml" > "$OUTPUT_DIR/hub-route.yaml"
else
  rm -f "$OUTPUT_DIR/hub.env"
  rm -f "$OUTPUT_DIR/hub-route.yaml"
fi
for name in librechat-admin-values librechat-admin-config librechat-admin-route; do
  if [[ -n "$CHAT_ADMIN_URL" ]]; then
    render "$DEX_DIR/templates/$name.yaml" > "$OUTPUT_DIR/$name.yaml"
  else
    rm -f "$OUTPUT_DIR/$name.yaml"
  fi
done

# Connector secrets are written as $NAME in the connector file (comments ignored).
CONNECTOR_VARS=()
while IFS= read -r name; do
  if [[ -n "$name" ]]; then CONNECTOR_VARS+=("$name"); fi
done < <(command grep -v '^[[:space:]]*#' "$CONNECTOR_FILE" | command grep -Eo '\$[A-Za-z_][A-Za-z0-9_]*' | sed 's/^\$//' | sort -u || true)
for name in ${CONNECTOR_VARS[@]+"${CONNECTOR_VARS[@]}"}; do
  [[ "$name" != LIBRECHAT_CLIENT_SECRET && "$name" != GRAFANA_CLIENT_SECRET && "$name" != HUB_CLIENT_SECRET ]] || fail "$name is reserved"
done

if [[ "$DRY_RUN" == true ]]; then
  printf '[DRY-RUN] Dex resources: %s\n[DRY-RUN] Generated configuration: %s\n' "$DEX_DIR" "$OUTPUT_DIR"
  printf '[DRY-RUN] Connector secrets expected in the environment: %s\n' "${CONNECTOR_VARS[*]:-none}"
  exit 0
fi

command -v kubectl >/dev/null 2>&1 || fail 'kubectl is required'
command -v openssl >/dev/null 2>&1 || fail 'openssl is required'
kubectl kustomize "$DEX_DIR" >/dev/null
kubectl --request-timeout=15s get --raw=/readyz >/dev/null || fail 'Cluster is not reachable'
if [[ -z "$HUB_URL" ]]; then
  existing_hub="$(kubectl -n dex get secret dex-secrets --ignore-not-found -o 'jsonpath={.data.HUB_CLIENT_SECRET}')"
  [[ -z "$existing_hub" ]] || fail 'Hub is configured; retain HUB_URL and HUB_GROUP to preserve its Dex client'
fi
if [[ -z "$CHAT_ADMIN_URL" ]]; then
  existing_admin="$(kubectl -n librechat get configmap librechat-admin-panel --ignore-not-found -o name)"
  [[ -z "$existing_admin" ]] || fail 'Admin panel is configured; retain CHAT_ADMIN_URL and CHAT_ADMIN_GROUP to preserve its callback and policy'
fi

secret_exists() {
  local found
  found="$(kubectl -n "$1" get secret "$2" --ignore-not-found -o name)"
  [[ -n "$found" ]]
}

secret_key() {
  kubectl -n "$1" get secret "$2" -o "jsonpath={.data.$3}" | base64 -d
}

# Reuse a stored value; only fall back when the Secret is verifiably absent.
stored_or() {
  local namespace="$1" secret="$2" key="$3" fallback="$4"
  if secret_exists "$namespace" "$secret"; then
    local current
    current="$(secret_key "$namespace" "$secret" "$key")"
    if [[ -n "$current" ]]; then printf '%s' "$current"; return; fi
  fi
  printf '%s' "$fallback"
}

apply_secret() {
  local namespace="$1" name="$2"
  kubectl -n "$namespace" create secret generic "$name" --from-env-file=/dev/stdin --dry-run=client -o yaml |
    kubectl apply -f - >/dev/null
}

for namespace in dex librechat observability; do
  kubectl create namespace "$namespace" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
done

chat_secret="$(stored_or dex dex-secrets LIBRECHAT_CLIENT_SECRET "$(openssl rand -hex 32)")"
grafana_secret="$(stored_or dex dex-secrets GRAFANA_CLIENT_SECRET "$(openssl rand -hex 32)")"
session_secret="$(stored_or librechat oidc-credentials session-secret "$(openssl rand -hex 32)")"
hub_secret=""
if [[ -n "$HUB_URL" ]]; then
  if secret_exists dex dex-secrets; then
    hub_secret="$(secret_key dex dex-secrets HUB_CLIENT_SECRET)"
  fi
  [[ -n "$hub_secret" ]] || hub_secret="$(openssl rand -hex 32)"
fi

{
  printf 'LIBRECHAT_CLIENT_SECRET=%s\nGRAFANA_CLIENT_SECRET=%s\n' "$chat_secret" "$grafana_secret"
  if [[ -n "$hub_secret" ]]; then printf 'HUB_CLIENT_SECRET=%s\n' "$hub_secret"; fi
  for name in ${CONNECTOR_VARS[@]+"${CONNECTOR_VARS[@]}"}; do
    value="${!name-}"
    [[ -n "$value" ]] || value="$(stored_or dex dex-secrets "$name" '')"
    [[ -n "$value" ]] || fail "Export $name in this terminal (referenced by $CONNECTOR_FILE)"
    printf '%s=%s\n' "$name" "$value"
  done
} | apply_secret dex dex-secrets
printf 'client-secret=%s\nsession-secret=%s\n' "$chat_secret" "$session_secret" | apply_secret librechat oidc-credentials
printf 'client-secret=%s\n' "$grafana_secret" | apply_secret observability oidc-credentials

kubectl -n dex create configmap dex-config --from-file=config.yaml="$OUTPUT_DIR/dex-config.yaml" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl apply -k "$DEX_DIR"
kubectl apply -f "$OUTPUT_DIR/dex-route.yaml"
kubectl -n dex rollout restart deployment/dex
kubectl -n dex rollout status deployment/dex --timeout=5m
printf '[INFO] Dex installed. App overrides: %s\n' "$OUTPUT_DIR"
printf '[INFO] When migrating from local accounts, remove the old config Secret: kubectl -n dex delete secret dex-config\n'
printf '[WARN] Models are NOT protected by this installer. Do not publish the model API until the machine-access policies are in place.\n'