#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT
mkdir -p "$TEST_DIR/bin"
export REAL_KUBECTL="$(command -v kubectl)"
export TEST_LOG="$TEST_DIR/commands.log"
export TEST_ADMIN_SECRET=absent TEST_ADMIN_ACTIVE=false
sed -e 's#example.com#zer0.test#g' -e 's#your-org#acme#g' "$ROOT_DIR/k8s/dex/auth.env.example" > "$TEST_DIR/auth.env"
printf '\nCHAT_ADMIN_URL=https://chatadmin.zer0.test\nCHAT_ADMIN_GROUP=acme:core\n' >> "$TEST_DIR/auth.env"
bash "$ROOT_DIR/scripts/install-auth.sh" --config "$TEST_DIR/auth.env" \
  --connector "$ROOT_DIR/k8s/dex/connectors/github.example.yaml" --output-dir "$TEST_DIR/auth" --dry-run >/dev/null
ln -s "$ROOT_DIR/scripts/tests/command-stub.sh" "$TEST_DIR/bin/helm"
export PATH="$TEST_DIR/bin:$PATH"
INSTALLER="$ROOT_DIR/k8s/addons/librechat/install-librechat.sh"

expect_failure() {
  if bash "$@" > "$TEST_DIR/failure.log" 2>&1; then
    printf 'Expected admin failure: %s\n' "$*" >&2
    exit 1
  fi
}

expect_failure "$INSTALLER" --with-admin-panel --dry-run
expect_failure "$INSTALLER" --auth-dir "$TEST_DIR/auth" --dry-run
bash "$INSTALLER" --auth-dir "$TEST_DIR/auth" --with-admin-panel --dry-run > "$TEST_DIR/rendered.yaml"
command grep -q -- "-f $TEST_DIR/auth/librechat-admin-values.yaml" "$TEST_LOG"
command grep -q 'image: ghcr.io/clickhouse/librechat-admin-panel@sha256:' "$TEST_DIR/rendered.yaml"
command grep -q 'SESSION_COOKIE_SECURE: "true"' "$TEST_DIR/rendered.yaml"
command grep -q 'ADMIN_SSO_ONLY: "true"' "$TEST_DIR/rendered.yaml"
command grep -q 'sectionName: https' "$TEST_DIR/rendered.yaml"
command grep -q 'name: librechat-admin-panel-session' "$TEST_DIR/rendered.yaml"
if command grep -q 'kind: Namespace' "$TEST_DIR/rendered.yaml"; then exit 1; fi

SCRIPT_DIR="$ROOT_DIR/k8s/addons/librechat"
source "$SCRIPT_DIR/admin.sh"
err() { printf '[ERROR] %s\n' "$*" >&2; }
NAMESPACE=librechat
AUTH_DIR="$TEST_DIR/auth"
ADMIN_DIR="$ROOT_DIR/k8s/addons/librechat-admin"
WITH_ADMIN_PANEL=true

kubectl() {
  local argument
  for argument in "$@"; do
    case "$argument" in
      --from-literal=*) printf '%s ' '--from-literal=REDACTED' >> "$TEST_LOG" ;;
      *) printf '%s ' "$argument" >> "$TEST_LOG" ;;
    esac
  done
  printf '\n' >> "$TEST_LOG"
  case "$*" in
    kustomize*) "$REAL_KUBECTL" "$@" ;;
    *'get secret librechat-admin-panel-session'*'-o name')
      [[ "$TEST_ADMIN_SECRET" != error ]] || return 1
      [[ "$TEST_ADMIN_SECRET" == absent ]] || printf secret/librechat-admin-panel-session
      ;;
    *'get secret librechat-admin-panel-session'*) printf '%s' "$TEST_ADMIN_SECRET" | base64 ;;
    *'get configmap librechat-admin-panel'*) [[ "$TEST_ADMIN_ACTIVE" != true ]] || printf configmap/librechat-admin-panel ;;
    *'get deployment librechat-admin-panel'*) ;;
    *'create secret'*'--from-env-file=/dev/stdin'*) command cat >/dev/null; printf 'kind: Secret\n' ;;
    *'get inferenceservice bge-embeddings'*) printf True ;;
    *'get secret oidc-credentials'*) ;;
    *'get secret'*) printf stable-test-credential-at-least-32-characters | base64 ;;
    *'create secret'*|*'create namespace'*) printf 'kind: Secret\n' ;;
    *'rollout status deployment/librechat-admin-panel'*) [[ "${TEST_ADMIN_ROLLOUT_FAIL:-false}" != true ]] ;;
    *'apply -f -'*) command cat >/dev/null ;;
  esac
}
export -f kubectl err

admin_preflight
[[ ${#ADMIN_SESSION_SECRET} -eq 64 ]]
: > "$TEST_LOG"
install_admin
command grep -q 'create secret generic librechat-admin-panel-session' "$TEST_LOG"
ready_line="$(command grep -n 'rollout status' "$TEST_LOG" | cut -d: -f1)"
route_line="$(command grep -n 'apply -f .*librechat-admin-route.yaml' "$TEST_LOG" | cut -d: -f1)"
[[ "$ready_line" -lt "$route_line" ]]
export TEST_ADMIN_SECRET=stable-session-key-at-least-thirty-two-characters
admin_preflight
[[ "$ADMIN_SESSION_SECRET" == "$TEST_ADMIN_SECRET" ]]
: > "$TEST_LOG"
install_admin
if command grep -q 'create secret' "$TEST_LOG"; then exit 1; fi
if command grep -q "$TEST_ADMIN_SECRET" "$TEST_LOG"; then exit 1; fi
for TEST_ADMIN_SECRET in short error; do
  if (admin_preflight) > "$TEST_DIR/failure.log" 2>&1; then exit 1; fi
done
WITH_ADMIN_PANEL=false
export TEST_ADMIN_ACTIVE=true
if (admin_preflight) > "$TEST_DIR/failure.log" 2>&1; then exit 1; fi
command grep -q 'preserve the admin policy' "$TEST_DIR/failure.log"
mkdir -p "$TEST_DIR/addons/librechat"
cp "$SCRIPT_DIR/install-librechat.sh" "$SCRIPT_DIR/admin.sh" "$SCRIPT_DIR/rag.sh" \
  "$SCRIPT_DIR/values.yaml" "$SCRIPT_DIR/httproute.yaml" "$TEST_DIR/addons/librechat/"
cp -R "$SCRIPT_DIR/rag" "$TEST_DIR/addons/librechat/"
cp -R "$ROOT_DIR/k8s/addons/librechat-admin" "$TEST_DIR/addons/"
INSTALLER="$TEST_DIR/addons/librechat/install-librechat.sh"
export TEST_ADMIN_SECRET=stable-session-key-at-least-thirty-two-characters
: > "$TEST_LOG"
bash "$INSTALLER" --auth-dir "$AUTH_DIR" --with-admin-panel > "$TEST_DIR/install.log"
helm_line="$(command grep -n 'helm upgrade' "$TEST_LOG" | cut -d: -f1)"
config_line="$(command grep -n 'apply -f .*librechat-admin-config.yaml' "$TEST_LOG" | cut -d: -f1)"
route_line="$(command grep -n 'apply -f .*librechat-admin-route.yaml' "$TEST_LOG" | cut -d: -f1)"
[[ "$helm_line" -lt "$config_line" && "$config_line" -lt "$route_line" ]]
command grep -q -- "-f $AUTH_DIR/librechat-admin-values.yaml" "$TEST_LOG"
if command grep -q "$TEST_ADMIN_SECRET" "$TEST_LOG" "$TEST_DIR/install.log"; then exit 1; fi

export TEST_ADMIN_SECRET=short
: > "$TEST_LOG"
expect_failure "$INSTALLER" --auth-dir "$AUTH_DIR" --with-admin-panel
if command grep -Eq 'helm upgrade|create namespace|apply -f' "$TEST_LOG"; then exit 1; fi
export TEST_ADMIN_SECRET=stable-session-key-at-least-thirty-two-characters TEST_ADMIN_ROLLOUT_FAIL=true
: > "$TEST_LOG"
expect_failure "$INSTALLER" --auth-dir "$AUTH_DIR" --with-admin-panel
if command grep -q 'apply -f .*librechat-admin-route.yaml' "$TEST_LOG"; then exit 1; fi
unset TEST_ADMIN_ROLLOUT_FAIL

sed '/^CHAT_ADMIN_/d' "$TEST_DIR/auth.env" > "$TEST_DIR/no-admin.env"
bash "$ROOT_DIR/scripts/install-auth.sh" --config "$TEST_DIR/no-admin.env" \
  --connector "$ROOT_DIR/k8s/dex/connectors/github.example.yaml" --output-dir "$TEST_DIR/disabled" --dry-run >/dev/null
: > "$TEST_LOG"
expect_failure "$INSTALLER" --auth-dir "$TEST_DIR/disabled"
command grep -q 'preserve the admin policy' "$TEST_DIR/failure.log"
if command grep -Eq 'helm upgrade|create namespace|apply -f' "$TEST_LOG"; then exit 1; fi
: > "$TEST_LOG"
expect_failure "$ROOT_DIR/scripts/install-auth.sh" --config "$TEST_DIR/no-admin.env" \
  --connector "$ROOT_DIR/k8s/dex/connectors/github.example.yaml" --output-dir "$TEST_DIR/disabled"
command grep -q 'retain CHAT_ADMIN_URL' "$TEST_DIR/failure.log"
if command grep -Eq 'create namespace|apply -f' "$TEST_LOG"; then exit 1; fi
printf 'PASS: admin rendering, full installer ordering, opt-in guards, secret lifecycle and rollout failures\n'