#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OVERLAY_PATH="$ROOT_DIR/k8s/overlays/dgx-spark"
TARGET_USER="${SUDO_USER:-}"
TARGET_HOME="${HOME}"
if [[ -n "$TARGET_USER" && "$TARGET_USER" != "root" ]]; then
  TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
fi
if [[ -z "${KUBECONFIG:-}" ]]; then
  export KUBECONFIG="${TARGET_HOME}/.kube/config"
fi

usage() {
  cat <<'EOF'
Deploy the enki.stack "all-in-one AI box" baseline on a DGX Spark running DGX OS.

Usage:
  ./scripts/deploy-dgx-spark.sh [options]

Options:
  --dry-run             Print commands without executing (forwarded to bootstrap scripts)
  --skip-k3s            Skip k3s install (use if k3s is already installed)
  --skip-gpu-operator   Skip GPU Operator install
  -h, --help            Show this help

Run this directly on the DGX Spark box (DGX OS), not remotely.

Examples:
  ./scripts/deploy-dgx-spark.sh
  ./scripts/deploy-dgx-spark.sh --dry-run
EOF
}

DRY_RUN="false"
SKIP_K3S="false"
SKIP_GPU_OPERATOR="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN="true"
      shift
      ;;
    --skip-k3s)
      SKIP_K3S="true"
      shift
      ;;
    --skip-gpu-operator)
      SKIP_GPU_OPERATOR="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[ERROR] Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

DRY_RUN_FLAG=()
if [[ "$DRY_RUN" == "true" ]]; then
  DRY_RUN_FLAG=(--dry-run)
fi

if [[ "$DRY_RUN" != "true" ]] && ! command -v kubectl >/dev/null 2>&1; then
  echo "[ERROR] kubectl is required" >&2
  exit 1
fi

if [[ "$SKIP_K3S" != "true" ]]; then
  echo "[INFO] Installing k3s"
  "$ROOT_DIR/infra/k3s/install-k3s.sh" "${DRY_RUN_FLAG[@]}"
fi

# Ahead of the GPU Operator: its DCGM ServiceMonitor needs the prometheus-operator
# CRDs, and the overlay later needs *Monitor + EnvoyProxy.
echo "[INFO] Installing monitoring stack (Prometheus + Grafana)"
"$ROOT_DIR/infra/monitoring/install-monitoring.sh" --platform dgx-spark "${DRY_RUN_FLAG[@]}"

if [[ "$SKIP_GPU_OPERATOR" != "true" ]]; then
  echo "[INFO] Installing NVIDIA GPU Operator"
  "$ROOT_DIR/infra/gpu-operator/install-gpu-operator.sh"
fi

echo "[INFO] Installing Envoy Gateway + Envoy AI Gateway controllers"
"$ROOT_DIR/infra/gateway/install-ai-gateway.sh"

if [[ "$DRY_RUN" == "true" ]]; then
  echo "[INFO] Dry-run mode detected; skipping kubectl apply."
  exit 0
fi

echo "[INFO] Applying DGX Spark overlay: $OVERLAY_PATH"
if command -v kustomize >/dev/null 2>&1; then
  kustomize build --load-restrictor=LoadRestrictionsNone "$OVERLAY_PATH" | kubectl apply -f -
else
  echo "[WARN] kustomize not found; using kubectl apply -k"
  kubectl kustomize --load-restrictor=LoadRestrictionsNone "$OVERLAY_PATH" | kubectl apply -f -
fi

echo "[INFO] Waiting for cert-manager deployment rollout"
kubectl -n cert-manager rollout status deploy/cert-manager --timeout=10m

echo "[INFO] Waiting for KServe controller rollout"
kubectl -n kserve rollout status deploy/kserve-controller-manager --timeout=10m || {
  echo "[WARN] KServe controller deployment name may differ by release; inspect with: kubectl -n kserve get deploy"
}

echo "[INFO] Deployment complete"
