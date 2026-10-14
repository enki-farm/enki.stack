#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT
mkdir -p "$TEST_DIR/bin" "$TEST_DIR/state"
for binary in helm kubectl; do
  ln -s "$ROOT_DIR/scripts/tests/gateway-command-stub.sh" "$TEST_DIR/bin/$binary"
done
export PATH="$TEST_DIR/bin:$PATH"
export TEST_LOG="$TEST_DIR/commands.log" TEST_STATE="$TEST_DIR/state"
INSTALLER="$ROOT_DIR/scripts/install-ai-gateway.sh"
: > "$TEST_LOG"
bash "$INSTALLER" --help >/dev/null
[[ ! -s "$TEST_LOG" ]]
for argument in --unknown --context; do
  if bash "$INSTALLER" "$argument" > "$TEST_DIR/output" 2>&1; then exit 1; fi
  [[ ! -s "$TEST_LOG" ]]
done
bash "$INSTALLER" > "$TEST_DIR/output"
command grep -q -- '--version v1.9.2' "$TEST_LOG"
command grep -q -- '--version v1.2.0' "$TEST_LOG"
! command grep -q -- '--force-conflicts' "$TEST_LOG"
! command grep -q 'rate-limit-values\|apply -k' "$TEST_LOG"
: > "$TEST_LOG"
bash "$INSTALLER" --context spark --enable-usage-rate-limiting --take-crd-ownership > "$TEST_DIR/output"
command grep -q -- '--force-conflicts' "$TEST_LOG"
command grep -q 'kubectl --context spark get storageclass local-path' "$TEST_LOG"
command grep -q 'helm --kube-context spark upgrade' "$TEST_LOG"
command grep -q 'envoy-gateway-rate-limit-values.yaml' "$TEST_LOG"
crd_line="$(command grep -n 'apply --server-side.*deployment.yaml' "$TEST_LOG" | cut -d: -f1)"
redis_line="$(command grep -n 'rollout status statefulset/redis' "$TEST_LOG" | cut -d: -f1)"
gateway_line="$(command grep -n 'upgrade --install eg ' "$TEST_LOG" | cut -d: -f1)"
[[ "$crd_line" -lt "$redis_line" && "$redis_line" -lt "$gateway_line" ]]
: > "$TEST_LOG"
export LIMITER_INSTALLED=true
bash "$INSTALLER" > "$TEST_DIR/output"
command grep -q 'envoy-gateway-rate-limit-values.yaml' "$TEST_LOG"
unset LIMITER_INSTALLED
: > "$TEST_LOG"
export FAIL_MATCH='get storageclass'
if bash "$INSTALLER" --enable-usage-rate-limiting > "$TEST_DIR/output" 2>&1; then exit 1; fi
! command grep -q 'upgrade\|apply' "$TEST_LOG"
unset FAIL_MATCH
printf 'PASS: gateway versions, context, CRD/Redis ordering, opt-in persistence and preflight failure\n'