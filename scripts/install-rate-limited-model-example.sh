#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_DIR="$ROOT_DIR/models/default-rate-limited"
ENV_FILE="$ROOT_DIR/.env.rate-limit-example"
CONTEXT=""
GATEWAY_URL="https://ai.zer0.garden"
CA_CERT=""
DRY_RUN=false
SECRET_NAME=default-rate-limited-api-keys
ROUTE_NAME=default-rate-limited-model
WORK_DIR=""
REMOVE_ROUTE=false

fail() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }
usage() {
  printf 'Usage: bash scripts/install-rate-limited-model-example.sh [--context NAME] [--env-file PATH] [--gateway-url HTTPS_URL] [--ca-cert PATH] [--dry-run]\n'
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --context|--env-file|--gateway-url|--ca-cert)
      [[ $# -ge 2 && -n "$2" && "$2" != -* ]] || fail "$1 requires a value"
      case "$1" in
        --context) CONTEXT="$2" ;;
        --env-file) ENV_FILE="$2" ;;
        --gateway-url) GATEWAY_URL="${2%/}" ;;
        --ca-cert) CA_CERT="$2" ;;
      esac
      shift 2
      ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "Unknown argument: $1" ;;
  esac
done

for binary in kubectl helm jq openssl curl; do
  command -v "$binary" >/dev/null || fail "$binary is required"
done
[[ "$GATEWAY_URL" == https://* ]] || fail "API keys require HTTPS"
[[ -z "$CA_CERT" || -f "$CA_CERT" ]] || fail "CA certificate not found"
if [[ "$DRY_RUN" == true ]]; then
  kubectl kustomize "$MODEL_DIR"
  printf '[INFO] Render only: no credentials generated and no cluster changes\n' >&2
  exit 0
fi
[[ -n "$CONTEXT" ]] || CONTEXT="$(kubectl config current-context)"
kube() { kubectl --context "$CONTEXT" "$@"; }
cleanup() {
  local exit_code=$?
  if [[ "$exit_code" -ne 0 && "$REMOVE_ROUTE" == true ]]; then
    kube -n inference delete aigatewayroute "$ROUTE_NAME" --ignore-not-found >/dev/null
  fi
  [[ -z "$WORK_DIR" ]] || rm -rf "$WORK_DIR"
  return "$exit_code"
}
trap cleanup EXIT
umask 077
WORK_DIR="$(mktemp -d)"
curl_args=(--silent --show-error --connect-timeout 10 --max-time 90)
[[ -z "$CA_CERT" ]] || curl_args+=(--cacert "$CA_CERT")

printf '[INFO] Checking example prerequisites on context %s\n' "$CONTEXT"
kube --request-timeout=15s get --raw=/readyz >/dev/null
kube -n inference wait inferenceservice/default --for=condition=Ready --timeout=60s
kube -n inference get aiservicebackend default-backend >/dev/null
kube -n envoy-ai-gateway-system wait gateway/envoy-ai-gateway-basic --for=condition=Programmed --timeout=60s
kube -n ai-gateway-ratelimit rollout status statefulset/redis --timeout=60s
kube -n envoy-gateway-system rollout status deployment/envoy-ratelimit --timeout=60s
helm --kube-context "$CONTEXT" -n envoy-gateway-system get values eg -o json |
  jq -e '.config.envoyGateway.rateLimit.failClosed == true and .config.envoyGateway.rateLimit.backend.type == "Redis"' >/dev/null ||
  fail "Install the fail-closed usage rate limiter first"
curl "${curl_args[@]}" --fail "$GATEWAY_URL/v1/models" -o /dev/null

kube -n inference get secret "$SECRET_NAME" --ignore-not-found -o json > "$WORK_DIR/existing-secret.json"
CLIENT_A=""
CLIENT_B=""
if [[ -s "$WORK_DIR/existing-secret.json" ]]; then
  CLIENT_A="$(jq -er '.data["demo-client-a"] | @base64d' "$WORK_DIR/existing-secret.json")"
  CLIENT_B="$(jq -er '.data["demo-client-b"] | @base64d' "$WORK_DIR/existing-secret.json")"
fi
if [[ -f "$ENV_FILE" ]]; then
  LOCAL_A=""
  LOCAL_B=""
  while IFS='=' read -r name value || [[ -n "$name" ]]; do
    case "$name" in
      DEMO_CLIENT_A_API_KEY) [[ -z "$LOCAL_A" ]] || fail "Duplicate client A"; LOCAL_A="$value" ;;
      DEMO_CLIENT_B_API_KEY) [[ -z "$LOCAL_B" ]] || fail "Duplicate client B"; LOCAL_B="$value" ;;
      ''|'#'*) ;;
      *) fail "Unexpected entry in credentials file" ;;
    esac
  done < "$ENV_FILE"
  [[ "$LOCAL_A" =~ ^[0-9a-f]{64}$ && "$LOCAL_B" =~ ^[0-9a-f]{64}$ ]] || fail "Expected two 64-character hexadecimal API keys"
  if [[ -n "$CLIENT_A" && ( "$CLIENT_A" != "$LOCAL_A" || "$CLIENT_B" != "$LOCAL_B" ) ]]; then
    fail "Local keys differ from the existing Secret; reconcile credentials explicitly before rotation"
  fi
  CLIENT_A="$LOCAL_A"
  CLIENT_B="$LOCAL_B"
else
  if [[ -z "$CLIENT_A" ]]; then
    CLIENT_A="$(openssl rand -hex 32)"
    CLIENT_B="$(openssl rand -hex 32)"
  fi
  [[ "$CLIENT_A" =~ ^[0-9a-f]{64}$ && "$CLIENT_B" =~ ^[0-9a-f]{64}$ ]] || fail "Existing demo keys have an unexpected format"
  mkdir -p "$(dirname "$ENV_FILE")"
  (set -o noclobber; printf 'DEMO_CLIENT_A_API_KEY=%s\nDEMO_CLIENT_B_API_KEY=%s\n' "$CLIENT_A" "$CLIENT_B" > "$ENV_FILE")
fi
[[ "$CLIENT_A" != "$CLIENT_B" ]] || fail "Clients require distinct keys"
chmod 600 "$ENV_FILE"
printf 'demo-client-a=%s\ndemo-client-b=%s\n' "$CLIENT_A" "$CLIENT_B" > "$WORK_DIR/keys.env"
kube -n inference create secret generic "$SECRET_NAME" --from-env-file="$WORK_DIR/keys.env" --dry-run=client -o yaml |
  kube apply -f - >/dev/null
kube apply -f "$MODEL_DIR/security-policy.yaml" -f "$MODEL_DIR/backend-traffic-policy.yaml"
if [[ -z "$(kube -n inference get aigatewayroute "$ROUTE_NAME" --ignore-not-found -o name)" ]]; then
  REMOVE_ROUTE=true
fi
kube apply -f "$MODEL_DIR/ai-gateway-route.yaml"
kube -n inference wait --for=condition=Accepted aigatewayroute/"$ROUTE_NAME" --timeout=90s
kube -n inference wait --for=jsonpath='{.status.parents[0].conditions[?(@.type=="Accepted")].status}'=True \
  httproute/"$ROUTE_NAME" --timeout=90s
for policy in securitypolicy/default-rate-limited-auth backendtrafficpolicy/default-rate-limited-tokens; do
  kube -n inference wait --for=jsonpath='{.status.ancestors[0].conditions[?(@.type=="Accepted")].status}'=True "$policy" --timeout=90s
done
printf '{"model":"default-rate-limited","messages":[{"role":"user","content":"Hello"}],"max_tokens":1}' > "$WORK_DIR/request.json"
printf 'header = "Authorization: Bearer %s"\n' "$CLIENT_A" > "$WORK_DIR/client-a.conf"
curl "${curl_args[@]}" --fail --retry 6 --retry-all-errors --retry-max-time 60 \
  --config "$WORK_DIR/client-a.conf" -H 'Content-Type: application/json' -H 'x-ai-eg-model: default-rate-limited' \
  --data-binary @"$WORK_DIR/request.json" -o "$WORK_DIR/response.json" "$GATEWAY_URL/v1/chat/completions"
HTTP_CODE="$(curl "${curl_args[@]}" -H 'Content-Type: application/json' -H 'x-ai-eg-model: default-rate-limited' --data-binary @"$WORK_DIR/request.json" \
  -o "$WORK_DIR/response.json" -w '%{http_code}' "$GATEWAY_URL/v1/chat/completions")"
[[ "$HTTP_CODE" == 401 ]] || fail "Expected unauthenticated denial (401), received $HTTP_CODE"
HTTP_CODE="$(curl "${curl_args[@]}" -H 'Content-Type: application/json' --data-binary @"$WORK_DIR/request.json" \
  -o "$WORK_DIR/response.json" -w '%{http_code}' "$GATEWAY_URL/v1/chat/completions")"
[[ "$HTTP_CODE" == 401 || "$HTTP_CODE" == 403 ]] || fail "Body-only request bypassed authentication: HTTP $HTTP_CODE"
REMOVE_ROUTE=false
printf '[INFO] Protected default-rate-limited alias installed. Keys: %s (mode 600).\n' "$ENV_FILE"