#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT
mkdir -p "$TEST_DIR/bin" "$TEST_DIR/state"
for binary in helm kubectl curl; do
  ln -s "$ROOT_DIR/scripts/tests/gateway-command-stub.sh" "$TEST_DIR/bin/$binary"
done
export PATH="$TEST_DIR/bin:$PATH"
export TEST_LOG="$TEST_DIR/commands.log" TEST_STATE="$TEST_DIR/state"
INSTALLER="$ROOT_DIR/scripts/install-rate-limited-model-example.sh"
ENV_FILE="$TEST_DIR/credentials.env"
: > "$TEST_LOG"
bash "$INSTALLER" --help >/dev/null
[[ ! -s "$TEST_LOG" ]]
bash "$INSTALLER" --env-file "$ENV_FILE" --dry-run > "$TEST_DIR/output"
[[ ! -f "$ENV_FILE" ]]
! command grep -q 'apply\|create secret\|get secret' "$TEST_LOG"
for argument in --unknown --context --env-file; do
  if bash "$INSTALLER" "$argument" > "$TEST_DIR/output" 2>&1; then exit 1; fi
done
if bash "$INSTALLER" --gateway-url http://insecure > "$TEST_DIR/output" 2>&1; then exit 1; fi
: > "$TEST_LOG"
export FAIL_MATCH='rollout status deployment/envoy-ratelimit'
if bash "$INSTALLER" --env-file "$ENV_FILE" > "$TEST_DIR/output" 2>&1; then exit 1; fi
[[ ! -f "$ENV_FILE" ]]
! command grep -q 'apply\|create secret' "$TEST_LOG"
unset FAIL_MATCH
bash "$INSTALLER" --context spark --env-file "$ENV_FILE" > "$TEST_DIR/output"
[[ -f "$ENV_FILE" && -f "$TEST_STATE/route" ]]
cp "$ENV_FILE" "$TEST_DIR/original.env"
command grep -q 'kubectl --context spark' "$TEST_LOG"
bash "$INSTALLER" --context spark --env-file "$ENV_FILE" > "$TEST_DIR/output"
cmp "$ENV_FILE" "$TEST_DIR/original.env"
rm "$ENV_FILE"
bash "$INSTALLER" --env-file "$ENV_FILE" > "$TEST_DIR/output"
cmp "$ENV_FILE" "$TEST_DIR/original.env"
while IFS='=' read -r name key; do
  ! command grep -Fq "$key" "$TEST_LOG"
  ! command grep -Fq "$key" "$TEST_DIR/output"
done < "$ENV_FILE"
printf 'DEMO_CLIENT_A_API_KEY=%064d\nDEMO_CLIENT_B_API_KEY=%064d\n' 1 2 > "$ENV_FILE"
: > "$TEST_LOG"
if bash "$INSTALLER" --env-file "$ENV_FILE" > "$TEST_DIR/output" 2>&1; then exit 1; fi
! command grep -q 'create secret\|apply' "$TEST_LOG"
cp "$TEST_DIR/original.env" "$ENV_FILE"
rm "$TEST_STATE/route"
export HTTP_CODE=200
if bash "$INSTALLER" --env-file "$ENV_FILE" > "$TEST_DIR/output" 2>&1; then exit 1; fi
[[ ! -f "$TEST_STATE/route" ]]
unset HTTP_CODE
printf 'PASS: example dry-run, context, prerequisite guards, key reuse/recovery/mismatch, secrecy and protection cleanup\n'