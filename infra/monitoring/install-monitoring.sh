#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VALUES_DIR="$ROOT_DIR/infra/monitoring"
DASHBOARDS_PATH="$ROOT_DIR/k8s/monitoring/dashboards"

NAMESPACE="observability"
STACK_RELEASE_NAME="kube-prometheus-stack"
GRAFANA_RELEASE_NAME="grafana"

# Pinned unlike the other infra scripts: prometheus-operator ships CRD changes in
# most minor releases, so a floating version can break a cluster on re-run.
STACK_CHART_VERSION="90.0.0"
GRAFANA_CHART_VERSION="13.2.2"

PLATFORM=""
DRY_RUN="false"
SKIP_CRDS="false"
DASHBOARDS_ONLY="false"

log() {
  printf '[INFO] %s\n' "$*"
}

warn() {
  printf '[WARN] %s\n' "$*"
}

err() {
  printf '[ERROR] %s\n' "$*" >&2
}

usage() {
  cat <<'EOF'
Install or update the monitoring layer (Prometheus + Grafana) of the stack.

Usage:
  ./infra/monitoring/install-monitoring.sh --platform <aks|dgx-spark> [options]

Options:
  --platform <name>             Target platform, selects the per-platform values
                                files (aks or dgx-spark). Required.
  --chart-version <ver>         Pin the kube-prometheus-stack chart version
  --grafana-chart-version <ver> Pin the grafana chart version
  --namespace <name>            Namespace for both releases (default: observability)
  --skip-crds                   Do not install/upgrade the prometheus-operator CRDs
  --dashboards-only             Only re-apply k8s/monitoring/dashboards, skip helm
  --dry-run                     Print commands without executing
  -h, --help                    Show this help

Installs two Helm releases so Prometheus and Grafana can be pinned separately:
  - kube-prometheus-stack: operator, CRDs, Prometheus, node-exporter,
    kube-state-metrics (Grafana and Alertmanager subcharts disabled)
  - grafana: Grafana with the dashboard/datasource sidecars enabled

Notes:
  - Run this BEFORE `kubectl apply -k` on an overlay: the ServiceMonitor,
    PodMonitor and EnvoyProxy CRDs must exist before the overlay references them.
  - Dashboards and datasources live in k8s/monitoring and are applied with the
    overlay, not by this script (except via --dashboards-only).

Examples:
  ./infra/monitoring/install-monitoring.sh --platform dgx-spark
  ./infra/monitoring/install-monitoring.sh --platform aks --dry-run
  ./infra/monitoring/install-monitoring.sh --platform dgx-spark --dashboards-only
EOF
}

require_bin() {
  local bin="$1"
  if ! command -v "$bin" >/dev/null 2>&1; then
    err "Required binary not found: $bin"
    exit 1
  fi
}

run_cmd() {
  if [[ "$DRY_RUN" == "true" ]]; then
    printf '[DRY-RUN] %s\n' "$*"
    return 0
  fi
  "$@"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --platform)
        PLATFORM="$2"
        shift 2
        ;;
      --chart-version)
        STACK_CHART_VERSION="$2"
        shift 2
        ;;
      --grafana-chart-version)
        GRAFANA_CHART_VERSION="$2"
        shift 2
        ;;
      --namespace)
        NAMESPACE="$2"
        shift 2
        ;;
      --skip-crds)
        SKIP_CRDS="true"
        shift
        ;;
      --dashboards-only)
        DASHBOARDS_ONLY="true"
        shift
        ;;
      --dry-run)
        DRY_RUN="true"
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        err "Unknown argument: $1"
        usage
        exit 1
        ;;
    esac
  done
}

validate_prereqs() {
  case "$PLATFORM" in
    aks|dgx-spark) ;;
    "")
      err "--platform is required (aks or dgx-spark)"
      usage
      exit 1
      ;;
    *)
      err "Unsupported platform: $PLATFORM (expected aks or dgx-spark)"
      exit 1
      ;;
  esac

  if [[ "$DRY_RUN" != "true" ]]; then
    require_bin kubectl
    if [[ "$DASHBOARDS_ONLY" != "true" ]]; then
      require_bin helm
      require_bin openssl
    fi
  fi

  if [[ "$DASHBOARDS_ONLY" == "true" ]]; then
    return
  fi

  local file
  for file in \
    "$VALUES_DIR/values-kube-prometheus-stack.yaml" \
    "$VALUES_DIR/values-kube-prometheus-stack-$PLATFORM.yaml" \
    "$VALUES_DIR/values-grafana.yaml" \
    "$VALUES_DIR/values-grafana-$PLATFORM.yaml"; do
    if [[ ! -f "$file" ]]; then
      err "Values file not found: $file"
      exit 1
    fi
  done
}

ensure_namespace() {
  log "Ensuring namespace exists: $NAMESPACE"
  if [[ "$DRY_RUN" == "true" ]]; then
    printf '[DRY-RUN] kubectl create namespace %s\n' "$NAMESPACE"
    return 0
  fi
  kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}

ensure_grafana_secret() {
  log "Ensuring Grafana admin credential exists"
  if [[ "$DRY_RUN" == "true" ]]; then
    printf '[DRY-RUN] kubectl -n %s create secret generic grafana-admin ...\n' "$NAMESPACE"
    return 0
  fi
  if kubectl -n "$NAMESPACE" get secret grafana-admin >/dev/null 2>&1; then
    # Pre-existing secrets from the old addon only carry admin-password.
    if ! kubectl -n "$NAMESPACE" get secret grafana-admin \
      -o jsonpath='{.data.admin-user}' | grep -q .; then
      log "Adding missing admin-user key to the existing grafana-admin secret"
      kubectl -n "$NAMESPACE" patch secret grafana-admin \
        --type merge -p '{"stringData":{"admin-user":"admin"}}'
    fi
    return 0
  fi
  kubectl -n "$NAMESPACE" create secret generic grafana-admin \
    --from-literal=admin-user="admin" \
    --from-literal=admin-password="$(openssl rand -base64 32 | tr -d '\n')"
}

add_helm_repos() {
  log "Adding/updating the prometheus-community and grafana-community Helm repos"
  run_cmd helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
  # grafana/grafana was deprecated in favour of this repo as of 2026-01-30.
  run_cmd helm repo add grafana-community https://grafana-community.github.io/helm-charts
  run_cmd helm repo update
}

install_prometheus_stack() {
  local helm_args=(upgrade --install "$STACK_RELEASE_NAME"
    prometheus-community/kube-prometheus-stack
    --namespace "$NAMESPACE" --create-namespace
    --version "$STACK_CHART_VERSION"
    -f "$VALUES_DIR/values-kube-prometheus-stack.yaml"
    -f "$VALUES_DIR/values-kube-prometheus-stack-$PLATFORM.yaml"
    --wait --timeout 10m)
  if [[ "$SKIP_CRDS" == "true" ]]; then
    helm_args+=(--set crds.enabled=false)
  fi

  log "Installing kube-prometheus-stack $STACK_CHART_VERSION (namespace: $NAMESPACE)"
  run_cmd helm "${helm_args[@]}"

  run_cmd kubectl wait --for=condition=Established \
    crd/prometheuses.monitoring.coreos.com \
    crd/servicemonitors.monitoring.coreos.com \
    crd/podmonitors.monitoring.coreos.com \
    --timeout=2m

  log "Waiting for the Prometheus statefulset rollout"
  run_cmd kubectl -n "$NAMESPACE" rollout status \
    "statefulset/prometheus-$STACK_RELEASE_NAME-prometheus" --timeout=10m || {
    warn "Prometheus statefulset name may differ by chart version; inspect with: kubectl -n $NAMESPACE get sts"
  }
}

install_grafana() {
  local helm_args=(upgrade --install "$GRAFANA_RELEASE_NAME" grafana-community/grafana
    --namespace "$NAMESPACE" --create-namespace
    --version "$GRAFANA_CHART_VERSION"
    -f "$VALUES_DIR/values-grafana.yaml"
    -f "$VALUES_DIR/values-grafana-$PLATFORM.yaml"
    --wait --timeout 10m)

  log "Installing Grafana $GRAFANA_CHART_VERSION (namespace: $NAMESPACE)"
  run_cmd helm "${helm_args[@]}"

  log "Waiting for the Grafana rollout"
  run_cmd kubectl -n "$NAMESPACE" rollout status \
    "deploy/$GRAFANA_RELEASE_NAME" --timeout=5m
}

apply_dashboards() {
  log "Applying dashboard ConfigMaps: $DASHBOARDS_PATH"
  # Server-side: node-exporter-full alone is ~468KB, over the 256KB limit for the
  # last-applied-configuration annotation that a client-side apply would write.
  run_cmd kubectl apply -k "$DASHBOARDS_PATH" --server-side --force-conflicts
}

main() {
  parse_args "$@"
  validate_prereqs

  if [[ "$DASHBOARDS_ONLY" == "true" ]]; then
    apply_dashboards
    log "Dashboard refresh complete"
    exit 0
  fi

  ensure_namespace
  ensure_grafana_secret
  add_helm_repos
  install_prometheus_stack
  install_grafana
  apply_dashboards
  log "Monitoring install complete (platform: $PLATFORM)"
}

main "$@"
