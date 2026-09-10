#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OVERLAY_PATH="$ROOT_DIR/k8s/overlays/dgx-spark"

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

if ! command -v kubectl >/dev/null 2>&1; then
  echo "[ERROR] kubectl is required" >&2
  exit 1
fi

if [[ "$SKIP_K3S" != "true" ]]; then
  echo "[INFO] Installing k3s"
  "$ROOT_DIR/infra/k3s/install-k3s.sh" "${DRY_RUN_FLAG[@]}"
fi

if [[ "$SKIP_GPU_OPERATOR" != "true" ]]; then
  echo "[INFO] Installing NVIDIA GPU Operator"
  "$ROOT_DIR/infra/k3s/install-gpu-operator.sh" "${DRY_RUN_FLAG[@]}"
fi

echo "[INFO] Installing Envoy Gateway controller"
"$ROOT_DIR/infra/gateway/install-envoy-gateway.sh" "${DRY_RUN_FLAG[@]}"

echo "[INFO] Installing Envoy AI Gateway controller"
"$ROOT_DIR/infra/gateway/install-ai-gateway.sh" "${DRY_RUN_FLAG[@]}"

if [[ "$DRY_RUN" == "true" ]]; then
  echo "[INFO] Dry-run mode detected; skipping kubectl apply."
  exit 0
fi

echo "[INFO] Ensuring Grafana admin credential exists"
kubectl create namespace observability --dry-run=client -o yaml | kubectl apply -f - >/dev/null
if ! kubectl -n observability get secret grafana-admin >/dev/null 2>&1; then
  kubectl -n observability create secret generic grafana-admin \
    --from-literal=admin-password="$(openssl rand -base64 32 | tr -d '\n')"
fi

echo "[INFO] Applying DGX Spark overlay: $OVERLAY_PATH"
kustomize build --load-restrictor=LoadRestrictionsNone "$OVERLAY_PATH" | kubectl apply -f -

echo "[INFO] Waiting for cert-manager deployment rollout"
kubectl -n cert-manager rollout status deploy/cert-manager --timeout=10m

echo "[INFO] Waiting for KServe controller rollout"
kubectl -n kserve rollout status deploy/kserve-controller-manager --timeout=10m || {
  echo "[WARN] KServe controller deployment name may differ by release; inspect with: kubectl -n kserve get deploy"
}

echo "[INFO] Waiting for Grafana rollout"
kubectl -n observability rollout status deploy/grafana --timeout=5m

echo "[INFO] Deployment complete"
