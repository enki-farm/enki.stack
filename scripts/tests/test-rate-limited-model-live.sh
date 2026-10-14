#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${1:-$ROOT_DIR/.env.rate-limit-example}"
GATEWAY_URL="${2:-https://ai.zer0.garden}"
[[ "$GATEWAY_URL" == https://* ]] || { printf 'HTTPS is required\n' >&2; exit 1; }
umask 077
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT
CLIENT_A=""
CLIENT_B=""
while IFS='=' read -r name value; do
  case "$name" in
    DEMO_CLIENT_A_API_KEY) CLIENT_A="$value" ;;
    DEMO_CLIENT_B_API_KEY) CLIENT_B="$value" ;;
    ''|'#'*) ;;
    *) printf 'Unexpected credentials entry\n' >&2; exit 1 ;;
  esac
done < "$ENV_FILE"
[[ "$CLIENT_A" =~ ^[0-9a-f]{64}$ && "$CLIENT_B" =~ ^[0-9a-f]{64}$ ]] || exit 1
printf 'header = "Authorization: Bearer %s"\n' "$CLIENT_A" > "$WORK_DIR/client-a.conf"
printf 'header = "Authorization: Bearer %s"\n' "$CLIENT_B" > "$WORK_DIR/client-b.conf"
printf 'header = "Authorization: Bearer invalid-demo-key"\n' > "$WORK_DIR/invalid.conf"
touch "$WORK_DIR/anonymous.conf"
jq -n '{model:"default-rate-limited",messages:[{role:"user",content:"Hello"}],max_tokens:1}' > "$WORK_DIR/small.json"
jq -n '{model:"default-rate-limited",messages:[{role:"user",content:([range(0;1200)|"hello"]|join(" "))}],max_tokens:1}' > "$WORK_DIR/large.json"
jq '. + {stream:true,stream_options:{include_usage:true}}' "$WORK_DIR/large.json" > "$WORK_DIR/stream.json"

request() {
  local client="$1" payload="$2" label="$3"
  shift 3
  HTTP_CODE="$(curl --silent --show-error --connect-timeout 10 --max-time 90 \
    --config "$WORK_DIR/$client.conf" -H 'Content-Type: application/json' \
    -H 'x-ai-eg-model: default-rate-limited' \
    --data-binary @"$WORK_DIR/$payload.json" "$@" \
    -o "$WORK_DIR/response" -w '%{http_code}' "${GATEWAY_URL%/}/v1/chat/completions")"
  printf '%s: HTTP %s\n' "$label" "$HTTP_CODE"
}
expect() { [[ "$HTTP_CODE" == "$1" ]] || { printf 'Expected HTTP %s\n' "$1" >&2; exit 1; }; }

request anonymous small 'Missing key'; expect 401
request invalid small 'Invalid key with forged identity' -H 'x-client-id: demo-client-b'; expect 401
request client-a small 'Client A initial request'; expect 200
request client-b small 'Client B initial request'; expect 200
request client-a large 'Client A large prompt'; expect 200
TOKENS="$(jq -er '.usage.total_tokens' "$WORK_DIR/response")"
[[ "$TOKENS" -gt 1000 ]] || { printf 'Test prompt consumed insufficient tokens\n' >&2; exit 1; }
printf 'Client A charged %s tokens\n' "$TOKENS"
request client-a small 'Client A exhausted'; expect 429
request client-a small 'Forged client B identity' -H 'x-client-id: demo-client-b'; expect 429
request client-a small 'Duplicate identity headers' -H 'x-client-id: fresh-client' -H 'x-client-id: demo-client-b'; expect 429
request client-a small 'Forged model header' -H 'x-ai-eg-model: default'; expect 403
request client-b small 'Client B isolated budget'; expect 200
request client-b stream 'Client B admitted stream'; expect 200
STREAM_TOKENS="$(command sed -n 's/^data: //p' "$WORK_DIR/response" | command grep -v '^\[DONE\]' | jq -s -er '[.[] | .usage.total_tokens? // empty] | last')"
[[ "$STREAM_TOKENS" -gt 1000 ]] || { printf 'Stream did not report sufficient final usage\n' >&2; exit 1; }
printf 'Client B streamed and charged %s tokens\n' "$STREAM_TOKENS"
request client-b small 'Client B exhausted after stream'; expect 429
printf 'PASS: API-key enforcement, independent token budgets, spoof protection and streaming accounting\n'