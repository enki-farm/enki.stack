#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONTEXT="${1:-spark}"
GATEWAY_URL="${2:-https://ai.zer0.garden}"
[[ "$GATEWAY_URL" == https://* ]] || exit 1
kube() { kubectl --context "$CONTEXT" "$@"; }
umask 077
WORK_DIR="$(mktemp -d)"
cleanup() {
  local exit_code=$?
  kube -n envoy-gateway-system delete networkpolicy enki-rate-limit-outage-test --ignore-not-found >/dev/null
  kube -n ai-gateway-ratelimit delete networkpolicy enki-rate-limit-outage-test --ignore-not-found >/dev/null
  kube apply -f "$ROOT_DIR/k8s/addons/ai-gateway-ratelimit/redis.yaml" >/dev/null
  kube -n ai-gateway-ratelimit rollout status statefulset/redis --timeout=2m >/dev/null
  rm -rf "$WORK_DIR"
  return "$exit_code"
}
trap cleanup EXIT
CLIENT_KEY="$(command sed -n 's/^DEMO_CLIENT_A_API_KEY=//p' "$ROOT_DIR/.env.rate-limit-example")"
[[ "$CLIENT_KEY" =~ ^[0-9a-f]{64}$ ]] || exit 1
printf 'header = "Authorization: Bearer %s"\n' "$CLIENT_KEY" > "$WORK_DIR/client.conf"
printf '{"model":"default-rate-limited","messages":[{"role":"user","content":"Hello"}],"max_tokens":1}' > "$WORK_DIR/request.json"
request() {
  HTTP_CODE="$(curl --silent --show-error --connect-timeout 5 --max-time 15 \
    --config "$WORK_DIR/client.conf" -H 'Content-Type: application/json' -H 'x-ai-eg-model: default-rate-limited' \
    --data-binary @"$WORK_DIR/request.json" -o "$WORK_DIR/response" -w '%{http_code}' "$GATEWAY_URL/v1/chat/completions")"
}
request
[[ "$HTTP_CODE" == 200 ]] || { printf 'Need a fresh minute budget before outage tests (HTTP %s)\n' "$HTTP_CODE" >&2; exit 1; }
printf 'Minute-window recovery: HTTP 200\n'
kube -n ai-gateway-ratelimit exec redis-0 -- redis-cli SET enki-rate-limit-persistence-check verified EX 180 >/dev/null
kube -n ai-gateway-ratelimit exec redis-0 -- redis-cli BGREWRITEAOF >/dev/null
kube -n ai-gateway-ratelimit delete pod redis-0 --wait=true >/dev/null
kube -n ai-gateway-ratelimit wait --for=create pod/redis-0 --timeout=60s
kube -n ai-gateway-ratelimit wait --for=condition=Ready pod/redis-0 --timeout=2m
[[ "$(kube -n ai-gateway-ratelimit exec redis-0 -- redis-cli GET enki-rate-limit-persistence-check)" == verified ]]
kube -n ai-gateway-ratelimit exec redis-0 -- redis-cli DEL enki-rate-limit-persistence-check >/dev/null
printf 'Redis AOF persistence survives pod replacement\n'

for component in ratelimit redis; do
  if [[ "$component" == ratelimit ]]; then
    namespace=envoy-gateway-system
    selector='app.kubernetes.io/component: ratelimit'
  else
    namespace=ai-gateway-ratelimit
    selector='app.kubernetes.io/name: ai-gateway-rate-limit-redis'
    kube -n "$namespace" patch networkpolicy redis-ingress --type=merge -p '{"spec":{"ingress":[]}}' >/dev/null
  fi
  printf 'apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: enki-rate-limit-outage-test\n  namespace: %s\nspec:\n  podSelector:\n    matchLabels:\n      %s\n  policyTypes: [Ingress]\n  ingress: []\n' "$namespace" "$selector" | kube apply -f - >/dev/null
  denied=false
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    request
    if [[ "$HTTP_CODE" == 500 ]]; then denied=true; break; fi
  done
  [[ "$denied" == true ]] || { printf '%s outage did not fail closed (HTTP %s)\n' "$component" "$HTTP_CODE" >&2; exit 1; }
  printf '%s network outage: HTTP 500\n' "$component"
  ORIGINAL_CODE="$(curl --silent --show-error --max-time 30 -H 'Content-Type: application/json' \
    -d '{"model":"default","messages":[{"role":"user","content":"Hello"}],"max_tokens":1}' \
    -o /dev/null -w '%{http_code}' "$GATEWAY_URL/v1/chat/completions")"
  [[ "$ORIGINAL_CODE" == 200 ]]
  printf 'Original default during %s outage: HTTP 200\n' "$component"
  kube -n "$namespace" delete networkpolicy enki-rate-limit-outage-test >/dev/null
  if [[ "$component" == redis ]]; then
    kube apply -f "$ROOT_DIR/k8s/addons/ai-gateway-ratelimit/redis.yaml" >/dev/null
  fi
  curl --silent --show-error --fail --retry 6 --retry-all-errors --retry-max-time 60 \
    --connect-timeout 5 --max-time 15 --config "$WORK_DIR/client.conf" \
    -H 'Content-Type: application/json' -H 'x-ai-eg-model: default-rate-limited' \
    --data-binary @"$WORK_DIR/request.json" -o /dev/null "$GATEWAY_URL/v1/chat/completions"
  printf '%s restored: HTTP 200\n' "$component"
done
printf 'PASS: minute reset, persistent Redis, limiter/Redis fail-closed behavior and unaffected default route\n'