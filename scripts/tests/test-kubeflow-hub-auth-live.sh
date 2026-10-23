#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONTEXT="${1:-spark}"
AUTH_DIR="${2:-$ROOT_DIR/.generated/auth}"
NAME="hub-auth-check-$(date +%s)"
HOST="$NAME.zer0.garden"
KUBECTL=(kubectl --context "$CONTEXT")
TEMP_DIR="$(mktemp -d)"
umask 077
cleanup() {
  "${KUBECTL[@]}" -n kubeflow delete httproute,securitypolicy,httproutefilter "$NAME" --ignore-not-found >/dev/null
  rm -rf "$TEMP_DIR"
}
trap cleanup EXIT
[[ -f "$AUTH_DIR/hub-route.yaml" ]]
for binary in node jq curl kubectl; do command -v "$binary" >/dev/null; done
issuer="$(sed -n 's/^DEX_ISSUER=//p' "$AUTH_DIR/hub.env")"
ISSUER="$issuer" node <<'JAVASCRIPT' > "$TEMP_DIR/keys.json"
const { generateKeyPairSync, sign } = require('node:crypto');
const { privateKey, publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
const jwk = { ...publicKey.export({ format: 'jwk' }), kid: 'isolated-hub-test', alg: 'RS256', use: 'sig' };
const encode = value => Buffer.from(JSON.stringify(value)).toString('base64url');
const now = Math.floor(Date.now() / 1000);
const token = overrides => {
  const claims = { iss: process.env.ISSUER, aud: 'hub', sub: 'hub-auth-test', email: 'hub-auth-test@example.invalid', groups: ['enki-farm:core'], iat: now, exp: now + 120, ...overrides };
  const payload = `${encode({ alg: 'RS256', kid: jwk.kid })}.${encode(claims)}`;
  return `${payload}.${sign('RSA-SHA256', Buffer.from(payload), privateKey).toString('base64url')}`;
};
console.log(JSON.stringify({ jwks: { keys: [jwk] }, tokens: { member: token({}), nonmember: token({ groups: ['other:team'] }), audience: token({ aud: 'other-app' }), expired: token({ exp: now - 60 }), noemail: token({ email: undefined }) } }));
JAVASCRIPT
"${KUBECTL[@]}" create --dry-run=client -f "$AUTH_DIR/hub-route.yaml" -o json |
  jq -s --arg name "$NAME" --arg host "$HOST" --slurpfile keys "$TEMP_DIR/keys.json" '
    .[] | .metadata.name = $name |
    if .kind == "SecurityPolicy" then
      del(.spec.oidc) | .spec.targetRefs[0].name = $name |
      .spec.jwt.providers[0] |= (del(.remoteJWKS) | .localJWKS = {type:"Inline",inline:($keys[0].jwks|tojson)})
    elif .kind == "HTTPRoute" then
      .spec.hostnames = [$host] |
      (.spec.rules[].filters[] | select(.type == "ExtensionRef") | .extensionRef.name) = $name
    else . end' > "$TEMP_DIR/probe.json"
"${KUBECTL[@]}" apply -f "$TEMP_DIR/probe.json" >/dev/null
"${KUBECTL[@]}" -n kubeflow wait "securitypolicy/$NAME" --for='jsonpath={.status.ancestors[0].conditions[?(@.type=="Accepted")].status}=True' --timeout=90s
gateway_ip="$("${KUBECTL[@]}" -n envoy-ai-gateway-system get gateway envoy-ai-gateway-basic -o 'jsonpath={.status.addresses[0].value}')"
request() {
  curl --silent --show-error --connect-timeout 5 --max-time 15 --resolve "$HOST:443:$gateway_ip" "$@" "https://$HOST/api/v1/user"
}
for test_case in member nonmember audience expired noemail; do
  jq -r --arg kind "$test_case" '"header = \"Authorization: Bearer " + .tokens[$kind] + "\""' "$TEMP_DIR/keys.json" > "$TEMP_DIR/curl.config"
  if [[ "$test_case" == member ]]; then
    request --config "$TEMP_DIR/curl.config" --retry 5 --retry-all-errors --retry-delay 1 --fail-with-body -H 'kubeflow-userid: spoofed@example.invalid' -H 'kubeflow-groups: enki-farm:core' -o "$TEMP_DIR/response.json" || { command cat "$TEMP_DIR/response.json" >&2; exit 1; }
    code=200
  else
    code="$(request --config "$TEMP_DIR/curl.config" -H 'kubeflow-userid: spoofed@example.invalid' -H 'kubeflow-groups: enki-farm:core' -o "$TEMP_DIR/response.json" -w '%{http_code}')"
  fi
  case "$test_case" in
    nonmember) [[ "$code" == 403 ]] || exit 1 ;;
    audience|expired) [[ "$code" == 401 || "$code" == 403 ]] || exit 1 ;;
    noemail) [[ "$code" == 400 ]] || exit 1 ;;
    member)
      jq -e '.data.userId == "hub-auth-test@example.invalid"' "$TEMP_DIR/response.json" >/dev/null || { command cat "$TEMP_DIR/response.json" >&2; exit 1; }
      curl --fail --silent --show-error --connect-timeout 5 --max-time 30 --resolve "$HOST:443:$gateway_ip" --config "$TEMP_DIR/curl.config" "https://$HOST/api/v1/namespaces" > "$TEMP_DIR/namespaces.json"
      jq -e '[.data[].name] == ["kubeflow"]' "$TEMP_DIR/namespaces.json" >/dev/null || { command cat "$TEMP_DIR/namespaces.json" >&2; exit 1; }
      curl --fail --silent --show-error --location --connect-timeout 5 --max-time 15 --resolve "$HOST:443:$gateway_ip" --config "$TEMP_DIR/curl.config" "https://$HOST/" > "$TEMP_DIR/landing.html"
      command grep -iq '<!doctype html' "$TEMP_DIR/landing.html"
      asset_path="$(sed -n 's/.*<script[^>]*src="\([^"]*\)".*/\1/p' "$TEMP_DIR/landing.html")"
      [[ "$asset_path" == /app.bundle.js ]] || { printf 'Expected standalone frontend script, got: %s\n' "$asset_path" >&2; exit 1; }
      curl --fail --silent --show-error --connect-timeout 5 --max-time 30 --resolve "$HOST:443:$gateway_ip" --config "$TEMP_DIR/curl.config" \
        -D "$TEMP_DIR/asset-headers" "https://$HOST$asset_path" > "$TEMP_DIR/app.js"
      command grep -Eiq '^content-type: (text|application)/(javascript|x-javascript)' "$TEMP_DIR/asset-headers"
      node --check "$TEMP_DIR/app.js"
      printf '[PASS] Frontend script resolves to JavaScript through the authenticated prefix rewrite\n'
      for job_namespace in kubeflow; do
        jq -n --arg ns "$job_namespace" '{data:{namespace:$ns,registryName:"model-registry-service",registryNamespace:"kubeflow"}}' > "$TEMP_DIR/registry-access-request.json"
        curl --fail --silent --show-error --connect-timeout 5 --max-time 30 --resolve "$HOST:443:$gateway_ip" --config "$TEMP_DIR/curl.config" \
          -H 'Content-Type: application/json' -d @"$TEMP_DIR/registry-access-request.json" \
          "https://$HOST/api/v1/check-namespace-registry-access" > "$TEMP_DIR/registry-access.json"
        jq -e '.data.hasAccess == true and .data.cannotCheck == false' "$TEMP_DIR/registry-access.json" >/dev/null
      done
      printf '[PASS] Only kubeflow is listed and its jobs pass the registry access check\n'
      curl --fail --silent --show-error --connect-timeout 5 --max-time 30 --resolve "$HOST:443:$gateway_ip" --config "$TEMP_DIR/curl.config" \
        "https://$HOST/api/v1/model_registry/model-registry-service/model_transfer_jobs?namespace=kubeflow&jobNamespace=kubeflow" > "$TEMP_DIR/transfer-jobs.json"
      jq -e '.data.items | type == "array"' "$TEMP_DIR/transfer-jobs.json" >/dev/null
      denied="$(curl --silent --show-error --connect-timeout 5 --max-time 30 --resolve "$HOST:443:$gateway_ip" --config "$TEMP_DIR/curl.config" \
        -o "$TEMP_DIR/default-jobs.json" -w '%{http_code}' \
        "https://$HOST/api/v1/model_registry/model-registry-service/model_transfer_jobs?namespace=kubeflow&jobNamespace=default")"
      [[ "$denied" == 403 ]]
      printf '[PASS] Namespace-scoped job lists work in kubeflow; default namespace access is denied\n'
      curl --fail --silent --show-error --connect-timeout 5 --max-time 15 --resolve "$HOST:443:$gateway_ip" --config "$TEMP_DIR/curl.config" "https://$HOST/api/v1/model_registry?namespace=kubeflow" > "$TEMP_DIR/registries.json"
      jq -e '.data | any(.name == "model-registry-service")' "$TEMP_DIR/registries.json" >/dev/null || { command cat "$TEMP_DIR/registries.json" >&2; exit 1; }
      curl --fail --silent --show-error --connect-timeout 5 --max-time 30 --resolve "$HOST:443:$gateway_ip" --config "$TEMP_DIR/curl.config" "https://$HOST/api/v1/model_catalog/sources?namespace=kubeflow" > "$TEMP_DIR/catalog-sources.json"
      jq -e '.data.items | any(.id == "huggingface")' "$TEMP_DIR/catalog-sources.json" >/dev/null || { command cat "$TEMP_DIR/catalog-sources.json" >&2; exit 1; }
      curl --fail --silent --show-error --connect-timeout 5 --max-time 30 --resolve "$HOST:443:$gateway_ip" --config "$TEMP_DIR/curl.config" "https://$HOST/api/v1/model_catalog/models?namespace=kubeflow&source=huggingface" > "$TEMP_DIR/catalog-models.json"
      jq -e '.data.items | type == "array"' "$TEMP_DIR/catalog-models.json" >/dev/null || { command cat "$TEMP_DIR/catalog-models.json" >&2; exit 1; }
      if [[ "${HUB_BROWSER_TEST:-false}" == true ]]; then
        HUB_BROWSER_URL="https://$HOST" HUB_BROWSER_IP="$gateway_ip" HUB_BROWSER_KEYS="$TEMP_DIR/keys.json" node "$ROOT_DIR/scripts/tests/hub-browser-smoke.cjs"
      fi
      ;;
  esac
  printf '[PASS] Native JWT %s check: HTTP %s\n' "$test_case" "$code"
done
anonymous="$(request -o /dev/null -w '%{http_code}')"
[[ "$anonymous" == 401 || "$anonymous" == 403 ]] || exit 1
printf '[PASS] Anonymous denial and verified identity override; isolated test keys and route removed on exit\n'
printf '[NOTE] This verifies the JWT/header portion, not Dex browser OIDC sessions or callbacks.\n'