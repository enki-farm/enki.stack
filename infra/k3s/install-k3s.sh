#!/usr/bin/env bash
set -euo pipefail

CLUSTER_ROLE="server"
K3S_VERSION=""
DISABLE_TRAEFIK="true"
NODE_IP=""
TLS_SAN=""
KUBECONFIG_PATH="${HOME}/.kube/config"

DRY_RUN="false"
SKIP_KUBECONFIG="false"

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
Install k3s locally on a single-node DGX Spark "all-in-one" box.

Usage:
  ./infra/k3s/install-k3s.sh [options]

Options:
  --k3s-version <version>   Pin INSTALL_K3S_VERSION (default: latest stable)
  --node-ip <ip>            Advertise this node IP (--node-ip flag to k3s)
  --tls-san <name>          Extra TLS SAN for the k3s API cert (e.g. a LAN hostname)
  --keep-traefik            Keep k3s' bundled Traefik instead of disabling it
  --kubeconfig-path <path>  Where to copy/merge the kubeconfig (default: ~/.kube/config)
  --skip-kubeconfig         Do not touch the local kubeconfig
  --dry-run                 Print commands without executing
  -h, --help                Show this help

Notes:
  - Traefik is disabled by default: Envoy Gateway (via the Gateway API) owns
    ingress in this repo. ServiceLB (klipper-lb) is kept so the Gateway's
    LoadBalancer Service gets a local IP on this single-node box.
  - Run this directly on the DGX Spark host (DGX OS), not remotely.

Examples:
  ./infra/k3s/install-k3s.sh
  ./infra/k3s/install-k3s.sh --dry-run
  ./infra/k3s/install-k3s.sh --tls-san dgx-spark.local
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
      --k3s-version)
        K3S_VERSION="$2"
        shift 2
        ;;
      --node-ip)
        NODE_IP="$2"
        shift 2
        ;;
      --tls-san)
        TLS_SAN="$2"
        shift 2
        ;;
      --keep-traefik)
        DISABLE_TRAEFIK="false"
        shift
        ;;
      --kubeconfig-path)
        KUBECONFIG_PATH="$2"
        shift 2
        ;;
      --skip-kubeconfig)
        SKIP_KUBECONFIG="true"
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
  require_bin curl
  require_bin kubectl
}

install_k3s() {
  local exec_args=("$CLUSTER_ROLE")

  if [[ "$DISABLE_TRAEFIK" == "true" ]]; then
    exec_args+=("--disable" "traefik")
  fi
  if [[ -n "$NODE_IP" ]]; then
    exec_args+=("--node-ip" "$NODE_IP")
  fi
  if [[ -n "$TLS_SAN" ]]; then
    exec_args+=("--tls-san" "$TLS_SAN")
  fi

  log "Installing k3s (${exec_args[*]})"
  if [[ "$DRY_RUN" == "true" ]]; then
    printf '[DRY-RUN] curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=%s INSTALL_K3S_EXEC="%s" sh -s -\n' \
      "${K3S_VERSION:-<latest>}" "${exec_args[*]}"
    return 0
  fi

  INSTALL_K3S_VERSION="$K3S_VERSION" INSTALL_K3S_EXEC="${exec_args[*]}" \
    bash -c 'curl -sfL https://get.k3s.io | sh -s -'
}

wait_for_node_ready() {
  log "Waiting for the k3s node to become Ready"
  run_cmd kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml wait node --for=condition=Ready --all --timeout=5m
}

setup_kubeconfig() {
  if [[ "$SKIP_KUBECONFIG" == "true" ]]; then
    log "Skipping kubeconfig setup (--skip-kubeconfig)"
    return 0
  fi

  log "Writing kubeconfig to $KUBECONFIG_PATH"
  if [[ "$DRY_RUN" == "true" ]]; then
    printf '[DRY-RUN] install -m 600 /etc/rancher/k3s/k3s.yaml %s\n' "$KUBECONFIG_PATH"
    return 0
  fi

  mkdir -p "$(dirname "$KUBECONFIG_PATH")"
  install -m 600 /etc/rancher/k3s/k3s.yaml "$KUBECONFIG_PATH"
  log "kubeconfig ready. If this is not the only kubeconfig you use, merge it manually via KUBECONFIG."
}

main() {
  parse_args "$@"
  validate_prereqs
  install_k3s
  if [[ "$DRY_RUN" != "true" ]]; then
    wait_for_node_ready
  fi
  setup_kubeconfig
  log "k3s install complete"
}

main "$@"
