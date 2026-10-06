#!/usr/bin/env bash
set -euo pipefail

COMMAND_NAME="${0##*/}"
printf '%s %s KUBECONFIG=%s\n' "$COMMAND_NAME" "$*" "${KUBECONFIG:-}" >> "$TEST_LOG"
if [[ "$COMMAND_NAME" == "${FAIL_COMMAND:-}" ]]; then
  exit 1
fi
if [[ "$COMMAND_NAME" == kubectl ]]; then
  case "$*" in
    'config current-context') printf 'test-context\n' ;;
    *'/readyz'*) [[ "${FAIL_CLUSTER:-false}" != true ]] || exit 1 ;;
    'apply -f -') command cat >/dev/null ;;
  esac
fi
if [[ "$COMMAND_NAME" == helm ]]; then
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == -f ]]; then
      [[ $# -ge 2 && -f "$2" ]] || exit 1
    fi
    shift
  done
fi