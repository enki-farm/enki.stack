#!/usr/bin/env bash
set -euo pipefail
COMMAND_NAME="${0##*/}"
printf '%s %s\n' "$COMMAND_NAME" "$*" >> "$TEST_LOG"
if [[ -n "${FAIL_MATCH:-}" && "$*" == *"$FAIL_MATCH"* ]]; then
  exit 1
fi
if [[ "$COMMAND_NAME" == kubectl ]]; then
  case "$*" in
    'config current-context') printf 'test-context\n' ;;
    *'get service redis --ignore-not-found'*) [[ "${LIMITER_INSTALLED:-false}" != true ]] || printf 'service/redis\n' ;;
    *'get secret default-rate-limited-api-keys'* )
      [[ ! -f "$TEST_STATE/secret.json" ]] || command cat "$TEST_STATE/secret.json"
      ;;
    *'create secret generic'* )
      for argument in "$@"; do
        if [[ "$argument" == --from-env-file=* ]]; then
          jq -Rn '[inputs | split("=") | {(.[0]): (.[1] | @base64)}] | {apiVersion:"v1",kind:"Secret",metadata:{name:"default-rate-limited-api-keys",namespace:"inference"},data:(add)}' < "${argument#*=}"
        fi
      done
      ;;
    *'get aigatewayroute default-rate-limited-model'* )
      [[ ! -f "$TEST_STATE/route" ]] || printf 'aigatewayroute/default-rate-limited-model\n'
      ;;
    *'apply -f -') command cat > "$TEST_STATE/secret.json" ;;
    *'apply --server-side'*' -f -') command cat >/dev/null ;;
    *'apply -f '*'/ai-gateway-route.yaml') touch "$TEST_STATE/route" ;;
    *'delete aigatewayroute'*) rm -f "$TEST_STATE/route" ;;
    *'kustomize'*) printf 'kind: List\nitems: []\n' ;;
  esac
fi
if [[ "$COMMAND_NAME" == helm ]]; then
  case "$*" in
    *'get values eg'*) printf '{"config":{"envoyGateway":{"rateLimit":{"failClosed":true,"backend":{"type":"Redis"}}}}}\n' ;;
    *'template eg-crds'*) printf 'kind: List\nitems: []\n' ;;
  esac
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == -f ]]; then
      [[ $# -ge 2 && -f "$2" ]] || exit 1
    fi
    shift
  done
fi
if [[ "$COMMAND_NAME" == curl ]]; then
  if [[ "$*" == *'/v1/chat/completions'* && "$*" == *'-w '* ]]; then
    printf '%s' "${HTTP_CODE:-401}"
  fi
fi