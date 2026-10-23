#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT
mkdir -p "$TEST_DIR/bin"
sed -e 's#example.com#zer0.test#' -e 's#your-org#acme#g' "$ROOT_DIR/k8s/dex/auth.env.example" > "$TEST_DIR/auth.env"
for connector in github entra; do
  bash "$ROOT_DIR/scripts/install-auth.sh" --config "$TEST_DIR/auth.env" \
    --connector "$ROOT_DIR/k8s/dex/connectors/$connector.example.yaml" \
    --output-dir "$TEST_DIR/$connector" --dry-run > "$TEST_DIR/$connector.log"
done
command grep -q 'GITHUB_CLIENT_ID GITHUB_CLIENT_SECRET' "$TEST_DIR/github.log"
command grep -q 'ENTRA_CLIENT_ID ENTRA_CLIENT_SECRET' "$TEST_DIR/entra.log"
command grep -q 'issuer: "https://auth.zer0.test"' "$TEST_DIR/github/dex-config.yaml"
command grep -q 'redirectURI: "https://auth.zer0.test/callback"' "$TEST_DIR/entra/dex-config.yaml"
command grep -q "contains(groups\[\*\], 'acme:observability-admins') && 'Admin'" "$TEST_DIR/github/grafana-values.yaml"
command grep -q 'OPENID_REQUIRED_ROLE: "acme:ai-users"' "$TEST_DIR/github/librechat-values.yaml"
command grep -q 'hostnames: \["chat.zer0.test"\]' "$TEST_DIR/github/librechat-route.yaml"
command grep -q 'id: hub' "$TEST_DIR/github/dex-config.yaml"
command grep -q 'secretEnv: HUB_CLIENT_SECRET' "$TEST_DIR/github/dex-config.yaml"
command grep -q 'https://hub.zer0.garden/oauth2/callback' "$TEST_DIR/github/dex-config.yaml"
command grep -q 'defaultAction: Deny' "$TEST_DIR/github/hub-route.yaml"
command grep -q 'values: \[enki-farm:core\]' "$TEST_DIR/github/hub-route.yaml"
command grep -q 'passThroughAuthHeader: false' "$TEST_DIR/github/hub-route.yaml"
command grep -q 'authorizationEndpoint: "https://auth.zer0.test/auth"' "$TEST_DIR/github/hub-route.yaml"
command grep -q 'tokenEndpoint: "https://auth.zer0.test/token"' "$TEST_DIR/github/hub-route.yaml"
[[ "$(command grep -c '^  namespace: kubeflow$' "$TEST_DIR/github/hub-route.yaml")" == 2 ]]
command grep -q 'name: model-registry-ui-service' "$TEST_DIR/github/hub-route.yaml"
if command grep -rq '@[A-Z_]*@' "$TEST_DIR/github"; then
  printf 'Unrendered placeholder in generated auth files\n' >&2
  exit 1
fi
AUTH_OUT="$TEST_DIR/github"
for binary in helm kubectl; do
  ln -s "$ROOT_DIR/scripts/tests/command-stub.sh" "$TEST_DIR/bin/$binary"
done
export PATH="$TEST_DIR/bin:$PATH"
export TEST_LOG="$TEST_DIR/commands.log"

bash "$ROOT_DIR/k8s/addons/librechat/install-librechat.sh" --auth-dir "$AUTH_OUT" --dry-run > "$TEST_DIR/chat.log"
command grep -q -- "-f $AUTH_OUT/librechat-values.yaml" "$TEST_LOG"
command grep -q -- "$AUTH_OUT/librechat-route.yaml" "$TEST_DIR/chat.log"
bash "$ROOT_DIR/scripts/install-monitoring.sh" --platform dgx-spark --auth-dir "$AUTH_OUT" --dry-run > "$TEST_DIR/grafana.log"
command grep -q -- "-f $AUTH_OUT/grafana-values.yaml" "$TEST_DIR/grafana.log"
command grep -q -- "$AUTH_OUT/grafana-route.yaml" "$TEST_DIR/grafana.log"

expect_failure() {
  if bash "$@" > "$TEST_DIR/failure.log" 2>&1; then
    printf 'Expected auth installer failure\n' >&2
    exit 1
  fi
}
expect_failure "$ROOT_DIR/scripts/install-auth.sh" --config
expect_failure "$ROOT_DIR/scripts/install-auth.sh" --config "$TEST_DIR/missing.env" --dry-run
sed 's#^CHAT_URL=.*#CHAT_URL=http://chat.zer0.test#' "$TEST_DIR/auth.env" > "$TEST_DIR/bad-url.env"
expect_failure "$ROOT_DIR/scripts/install-auth.sh" --config "$TEST_DIR/bad-url.env" \
  --connector "$ROOT_DIR/k8s/dex/connectors/github.example.yaml" --dry-run
sed 's#^CHAT_GROUP=.*#CHAT_GROUP=bad group#' "$TEST_DIR/auth.env" > "$TEST_DIR/bad-group.env"
expect_failure "$ROOT_DIR/scripts/install-auth.sh" --config "$TEST_DIR/bad-group.env" \
  --connector "$ROOT_DIR/k8s/dex/connectors/github.example.yaml" --dry-run
expect_failure "$ROOT_DIR/k8s/addons/librechat/install-librechat.sh" --auth-dir
expect_failure "$ROOT_DIR/k8s/addons/librechat/install-librechat.sh" --auth-dir "$TEST_DIR/missing" --dry-run
expect_failure "$ROOT_DIR/k8s/addons/librechat/install-librechat.sh" --namespace other --auth-dir "$AUTH_OUT" --dry-run
expect_failure "$ROOT_DIR/scripts/install-monitoring.sh" --platform aks --namespace other --auth-dir "$AUTH_OUT" --dry-run
expect_failure "$ROOT_DIR/scripts/install-monitoring.sh" --platform aks --dashboards-only --auth-dir "$AUTH_OUT" --dry-run
export AUTH_INSTALLED=true
expect_failure "$ROOT_DIR/k8s/addons/librechat/install-librechat.sh"
command grep -q 'avoid restoring legacy login' "$TEST_DIR/failure.log"
expect_failure "$ROOT_DIR/scripts/install-monitoring.sh" --platform aks
command grep -q 'avoid restoring legacy login' "$TEST_DIR/failure.log"
unset AUTH_INSTALLED
printf '\nCHAT_ADMIN_URL=https://chatadmin.zer0.test\nCHAT_ADMIN_GROUP=acme:core\n' >> "$TEST_DIR/auth.env"
bash "$ROOT_DIR/scripts/install-auth.sh" --config "$TEST_DIR/auth.env" \
  --connector "$ROOT_DIR/k8s/dex/connectors/github.example.yaml" --output-dir "$TEST_DIR/admin" --dry-run
command grep -Fq '"https://chat.zer0.test/oauth/openid/callback", "https://chat.zer0.test/api/admin/oauth/openid/callback"' "$TEST_DIR/admin/dex-config.yaml"
command grep -q 'OPENID_ADMIN_ROLE: "acme:core"' "$TEST_DIR/admin/librechat-admin-values.yaml"
command grep -q 'OPENID_REQUIRED_ROLE: "acme:ai-users"' "$TEST_DIR/admin/librechat-values.yaml"
command grep -q 'hostnames: \["chatadmin.zer0.test"\]' "$TEST_DIR/admin/librechat-admin-route.yaml"
command grep -q 'ADMIN_SSO_ONLY: "true"' "$TEST_DIR/admin/librechat-admin-config.yaml"
if command grep -rq '@[A-Z_]*@' "$TEST_DIR/admin"; then exit 1; fi
for replacement in 'CHAT_ADMIN_URL=http://admin.test' 'CHAT_ADMIN_URL=https://CHAT.zer0.test' 'CHAT_ADMIN_GROUP=' 'CHAT_ADMIN_GROUP=bad group'; do
  key="${replacement%%=*}"
  sed "s#^$key=.*#$replacement#" "$TEST_DIR/auth.env" > "$TEST_DIR/bad-admin.env"
  expect_failure "$ROOT_DIR/scripts/install-auth.sh" --config "$TEST_DIR/bad-admin.env" \
    --connector "$ROOT_DIR/k8s/dex/connectors/github.example.yaml" --output-dir "$TEST_DIR/admin" --dry-run
done
sed '/^CHAT_ADMIN_/d' "$TEST_DIR/auth.env" > "$TEST_DIR/no-admin.env"
bash "$ROOT_DIR/scripts/install-auth.sh" --config "$TEST_DIR/no-admin.env" \
  --connector "$ROOT_DIR/k8s/dex/connectors/github.example.yaml" --output-dir "$TEST_DIR/admin" --dry-run
[[ ! -f "$TEST_DIR/admin/librechat-admin-values.yaml" ]]
if command grep -q '/api/admin/' "$TEST_DIR/admin/dex-config.yaml"; then exit 1; fi
printf 'PASS: rendered Dex and app configuration for GitHub and Entra, opt-in app overrides, namespace guards and invalid arguments\n'