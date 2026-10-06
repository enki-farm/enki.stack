#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT
mkdir -p "$TEST_DIR/stack/scripts" "$TEST_DIR/bin"
cp "$ROOT_DIR/scripts/deploy.sh" "$TEST_DIR/stack/scripts/deploy.sh"
ln -s "$ROOT_DIR/helm" "$TEST_DIR/stack/helm"
ln -s "$ROOT_DIR/k8s" "$TEST_DIR/stack/k8s"
for installer in monitoring gpu-operator ai-gateway kserve; do
  ln -s "$ROOT_DIR/scripts/tests/command-stub.sh" "$TEST_DIR/stack/scripts/install-$installer.sh"
done
for binary in kubectl helm openssl kustomize; do
  ln -s "$ROOT_DIR/scripts/tests/command-stub.sh" "$TEST_DIR/bin/$binary"
done
export PATH="$TEST_DIR/bin:/usr/bin:/bin"
export TEST_LOG="$TEST_DIR/commands.log"
export KUBECONFIG="$TEST_DIR/unchanged-kubeconfig"
DEPLOY="$TEST_DIR/stack/scripts/deploy.sh"
cd "$TEST_DIR"

run_success() {
  : > "$TEST_LOG"
  bash "$DEPLOY" "$@" > "$TEST_DIR/output.log" 2>&1
}

run_failure() {
  : > "$TEST_LOG"
  if bash "$DEPLOY" "$@" > "$TEST_DIR/output.log" 2>&1; then
    printf 'Expected failure: %s\n' "$*" >&2
    exit 1
  fi
}

run_success --help
[[ ! -s "$TEST_LOG" ]]
for argument in '' '--platform' '--platform unknown' '--dry-run' '--skip-k3s' '--location westeurope' '--platform aks --skip-gpu-operator'; do
  read -r -a arguments <<< "$argument"
  run_failure ${arguments[@]+"${arguments[@]}"}
  [[ ! -s "$TEST_LOG" ]]
done

for platform in aks dgx-spark; do
  run_success --platform "$platform"
  expected=(install-monitoring.sh install-ai-gateway.sh install-kserve.sh)
  if [[ "$platform" == dgx-spark ]]; then
    expected=(install-monitoring.sh install-gpu-operator.sh install-ai-gateway.sh install-kserve.sh)
  fi
  actual=()
  while read -r command_name remainder; do
    case "$command_name" in install-*) actual+=("$command_name") ;; esac
    [[ "$remainder" == *"KUBECONFIG=$KUBECONFIG" ]]
  done < "$TEST_LOG"
  [[ "${actual[*]}" == "${expected[*]}" ]]
  command grep -q "install-monitoring.sh --platform $platform" "$TEST_LOG"
  command grep -q "kustomize build .*overlays/$platform" "$TEST_LOG"
  [[ ! -e "$KUBECONFIG" ]]
done

run_success --platform dgx-spark --skip-gpu-operator
! command grep -q install-gpu-operator "$TEST_LOG"
export FAIL_CLUSTER=true
run_failure --platform aks
! command grep -q install- "$TEST_LOG"
unset FAIL_CLUSTER
export FAIL_COMMAND=install-monitoring.sh
run_failure --platform dgx-spark
! command grep -q install-ai-gateway "$TEST_LOG"
unset FAIL_COMMAND
rm "$TEST_DIR/bin/kustomize"
run_success --platform aks
command grep -q 'kubectl kustomize --load-restrictor=LoadRestrictionsNone' "$TEST_LOG"
rm "$TEST_DIR/bin/helm"
run_failure --platform aks
[[ ! -s "$TEST_LOG" ]]
ln -s "$ROOT_DIR/scripts/tests/command-stub.sh" "$TEST_DIR/bin/helm"

for installer in ai-gateway gpu-operator; do
  bash "$ROOT_DIR/scripts/install-$installer.sh" > "$TEST_DIR/output.log" 2>&1
done
bash "$ROOT_DIR/scripts/install-monitoring.sh" --platform aks --dry-run > "$TEST_DIR/output.log"
bash "$ROOT_DIR/scripts/install-monitoring.sh" --platform dgx-spark --dry-run > "$TEST_DIR/output.log"
bash "$ROOT_DIR/scripts/install-kserve.sh" --dry-run > "$TEST_DIR/output.log"
printf 'PASS: deployment arguments, order, context, failures, fallback and installer paths\n'