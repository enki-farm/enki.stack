#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLATFORM=""
SKIP_GPU_OPERATOR="false"

usage() {
  cat <<'EOF'
Install enki.stack on an existing Kubernetes cluster using the current context.

Usage:
  ./scripts/deploy.sh --platform <aks|dgx-spark> [--skip-gpu-operator]

Options:
  --platform <name>     Required; selects Helm values and Kubernetes overlay
  --skip-gpu-operator   DGX Spark only; use existing GPU management
  -h, --help            Show this help

Cluster provisioning and Cloudflared are managed separately in enki.infra.
This command does not support --dry-run or cluster-provisioning options.
EOF
}

fail() {
  printf '[ERROR] %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --platform)
      [[ $# -ge 2 && "$2" != --* ]] || fail "--platform requires aks or dgx-spark"
      PLATFORM="$2"
      shift 2
      ;;
    --skip-gpu-operator)
      SKIP_GPU_OPERATOR="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *) fail "Unknown argument: $1 (see --help)" ;;
  esac
done

case "$PLATFORM" in
  aks|dgx-spark) ;;
  *) fail "--platform must be aks or dgx-spark" ;;
esac
if [[ "$PLATFORM" == aks && "$SKIP_GPU_OPERATOR" == true ]]; then
  fail "--skip-gpu-operator is only supported for dgx-spark"
fi

for binary in kubectl helm openssl; do
  command -v "$binary" >/dev/null 2>&1 || fail "$binary is required"
done
OVERLAY_PATH="$ROOT_DIR/k8s/overlays/$PLATFORM"
[[ -f "$OVERLAY_PATH/kustomization.yaml" ]] || fail "Overlay not found: $OVERLAY_PATH"
for component in monitoring gateway kserve; do
  [[ -d "$ROOT_DIR/helm/$component" ]] || fail "Helm configuration not found: $component"
done

CONTEXT="$(kubectl config current-context)" || fail "No current Kubernetes context"
printf '[INFO] Deploying platform %s to context %s\n' "$PLATFORM" "$CONTEXT"
kubectl --request-timeout=15s get --raw=/readyz >/dev/null || fail "Kubernetes API is not ready"
kubectl wait node --all --for=condition=Ready --timeout=60s || fail "Cluster nodes are not ready"

if command -v kustomize >/dev/null 2>&1; then
  RENDER_COMMAND=(kustomize build --load-restrictor=LoadRestrictionsNone)
else
  kubectl kustomize --help >/dev/null || fail "Kustomize support is required"
  RENDER_COMMAND=(kubectl kustomize --load-restrictor=LoadRestrictionsNone)
fi

"$ROOT_DIR/scripts/install-monitoring.sh" --platform "$PLATFORM"
if [[ "$PLATFORM" == dgx-spark && "$SKIP_GPU_OPERATOR" != true ]]; then
  "$ROOT_DIR/scripts/install-gpu-operator.sh"
fi
"$ROOT_DIR/scripts/install-ai-gateway.sh"
"$ROOT_DIR/scripts/install-kserve.sh"

printf '[INFO] Applying overlay: %s\n' "$OVERLAY_PATH"
"${RENDER_COMMAND[@]}" "$OVERLAY_PATH" | kubectl apply -f -
kubectl -n cert-manager rollout status deploy/cert-manager --timeout=10m
kubectl -n kserve rollout status deploy/kserve-controller-manager --timeout=10m || {
  printf '[WARN] KServe controller deployment name may differ; inspect kubectl -n kserve get deploy\n'
}
printf '[INFO] Deployment complete\n'