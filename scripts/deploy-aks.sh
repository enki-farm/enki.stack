#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AKS_SCRIPT="$ROOT_DIR/infra/aks/create-aks.sh"
OVERLAY_PATH="$ROOT_DIR/k8s/overlays/aks"

usage() {
  cat <<'EOF'
Deploy enki.infer AKS baseline (production / larger deployments).

Usage:
  ./scripts/deploy-aks.sh [bootstrap-options]

Examples:
  ./scripts/deploy-aks.sh
  ./scripts/deploy-aks.sh --location westeurope --dry-run
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ ! -x "$AKS_SCRIPT" ]]; then
  echo "[ERROR] AKS script is missing or not executable: $AKS_SCRIPT" >&2
  exit 1
fi

if ! command -v kubectl >/dev/null 2>&1; then
  echo "[ERROR] kubectl is required" >&2
  exit 1
fi

echo "[INFO] Bootstrapping AKS cluster"
"$AKS_SCRIPT" "$@"

if [[ "${*:-}" == *"--dry-run"* ]]; then
  echo "[INFO] Dry-run mode detected; skipping helm/kubectl apply."
  exit 0
fi

echo "[INFO] Installing Envoy Gateway controller"
"$ROOT_DIR/infra/gateway/install-envoy-gateway.sh"

echo "[INFO] Installing Envoy AI Gateway controller"
"$ROOT_DIR/infra/gateway/install-ai-gateway.sh"

# Must precede the overlay apply: the overlay references *Monitor and EnvoyProxy CRDs.
echo "[INFO] Installing monitoring stack (Prometheus + Grafana)"
"$ROOT_DIR/infra/monitoring/install-monitoring.sh" --platform aks

echo "[INFO] Applying AKS overlay: $OVERLAY_PATH"
kustomize build --load-restrictor=LoadRestrictionsNone "$OVERLAY_PATH" | kubectl apply -f -

echo "[INFO] Waiting for cert-manager deployment rollout"
kubectl -n cert-manager rollout status deploy/cert-manager --timeout=10m

echo "[INFO] Waiting for KServe controller rollout"
kubectl -n kserve rollout status deploy/kserve-controller-manager --timeout=10m || {
  echo "[WARN] KServe controller deployment name may differ by release; inspect with: kubectl -n kserve get deploy"
}

echo "[INFO] Deployment complete"
