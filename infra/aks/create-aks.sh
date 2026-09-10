#!/usr/bin/env bash
set -euo pipefail

SUBSCRIPTION_ID="8e270df5-1ea3-4c80-8880-ed961adc744a"
RESOURCE_GROUP="rg-enki-stack"
CLUSTER_NAME="aks-enki-stack"
LOCATION="westeurope"
ACR_NAME="enkifarm"

SYSTEM_POOL_NAME="system"
SYSTEM_NODE_COUNT="1"
SYSTEM_NODE_VM_SIZE="Standard_D4s_v5"

USER_POOL_NAME="usernp"
USER_NODE_COUNT="1"
USER_NODE_VM_SIZE="Standard_D4s_v5"

ENABLE_GPU_POOL="false"
GPU_POOL_NAME="gpunp"
GPU_NODE_COUNT="0"
GPU_NODE_VM_SIZE="Standard_NC8as_T4_v3"

K8S_VERSION=""
POD_CIDR="192.168.0.0/16"
SERVICE_CIDR="10.0.0.0/16"
DNS_SERVICE_IP="10.0.0.10"

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
Create or reconcile a dev AKS cluster for enki.infer.

Usage:
  ./infra/aks/create-aks.sh [options]

Options:
  --subscription-id <id>       Azure subscription ID (default: preset value)
  --resource-group <name>      Resource group name (default: rg-enki-infer)
  --cluster-name <name>        AKS cluster name (default: aks-enki-infer)
  --location <region>          Azure region (default: westeurope)
  --acr-name <name>            ACR name to attach to AKS (default: enkifarm)
  --k8s-version <version>      Optional Kubernetes version pin (default: latest supported)

  --system-node-count <n>      System pool node count (default: 1)
  --system-node-vm-size <sku>  System pool VM size (default: Standard_D4s_v5)
  --user-pool-name <name>      User pool name (default: usernp)
  --user-node-count <n>        User pool node count (default: 1)
  --user-node-vm-size <sku>    User pool VM size (default: Standard_D4s_v5)

  --enable-gpu-pool            Enable creation of a GPU user pool (default: disabled)
  --gpu-pool-name <name>       GPU pool name (default: gpunp)
  --gpu-node-count <n>         GPU pool node count (default: 0)
  --gpu-node-vm-size <sku>     GPU pool VM size (default: Standard_NC8as_T4_v3)

  --pod-cidr <cidr>            Pod CIDR for Azure CNI overlay (default: 192.168.0.0/16)
  --service-cidr <cidr>        Service CIDR (default: 10.0.0.0/16)
  --dns-service-ip <ip>        DNS service IP (default: 10.0.0.10)

  --dry-run                    Print commands without executing
  --skip-kubeconfig            Do not run az aks get-credentials
  -h, --help                   Show this help

Examples:
  ./infra/aks/create-aks.sh
  ./infra/aks/create-aks.sh --location westeurope --enable-gpu-pool
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
      --subscription-id)
        SUBSCRIPTION_ID="$2"
        shift 2
        ;;
      --resource-group)
        RESOURCE_GROUP="$2"
        shift 2
        ;;
      --cluster-name)
        CLUSTER_NAME="$2"
        shift 2
        ;;
      --location)
        LOCATION="$2"
        shift 2
        ;;
      --acr-name)
        ACR_NAME="$2"
        shift 2
        ;;
      --k8s-version)
        K8S_VERSION="$2"
        shift 2
        ;;
      --system-node-count)
        SYSTEM_NODE_COUNT="$2"
        shift 2
        ;;
      --system-node-vm-size)
        SYSTEM_NODE_VM_SIZE="$2"
        shift 2
        ;;
      --user-pool-name)
        USER_POOL_NAME="$2"
        shift 2
        ;;
      --user-node-count)
        USER_NODE_COUNT="$2"
        shift 2
        ;;
      --user-node-vm-size)
        USER_NODE_VM_SIZE="$2"
        shift 2
        ;;
      --enable-gpu-pool)
        ENABLE_GPU_POOL="true"
        shift
        ;;
      --gpu-pool-name)
        GPU_POOL_NAME="$2"
        shift 2
        ;;
      --gpu-node-count)
        GPU_NODE_COUNT="$2"
        shift 2
        ;;
      --gpu-node-vm-size)
        GPU_NODE_VM_SIZE="$2"
        shift 2
        ;;
      --pod-cidr)
        POD_CIDR="$2"
        shift 2
        ;;
      --service-cidr)
        SERVICE_CIDR="$2"
        shift 2
        ;;
      --dns-service-ip)
        DNS_SERVICE_IP="$2"
        shift 2
        ;;
      --dry-run)
        DRY_RUN="true"
        shift
        ;;
      --skip-kubeconfig)
        SKIP_KUBECONFIG="true"
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
  require_bin az
  require_bin kubectl
  require_bin kustomize

  if [[ "$DRY_RUN" == "false" ]]; then
    if ! az account show >/dev/null 2>&1; then
      err "Azure CLI is not logged in. Run: az login"
      exit 1
    fi
  fi
}

resource_group_exists() {
  az group exists --name "$RESOURCE_GROUP"
}

cluster_exists() {
  az aks show --resource-group "$RESOURCE_GROUP" --name "$CLUSTER_NAME" >/dev/null 2>&1
}

node_pool_exists() {
  local pool_name="$1"
  az aks nodepool show \
    --resource-group "$RESOURCE_GROUP" \
    --cluster-name "$CLUSTER_NAME" \
    --name "$pool_name" >/dev/null 2>&1
}

create_resource_group() {
  if [[ "$(resource_group_exists)" == "true" ]]; then
    log "Resource group already exists: $RESOURCE_GROUP"
    return
  fi

  log "Creating resource group: $RESOURCE_GROUP ($LOCATION)"
  run_cmd az group create --name "$RESOURCE_GROUP" --location "$LOCATION" >/dev/null
}

create_or_reuse_cluster() {
  if cluster_exists; then
    log "AKS cluster already exists: $CLUSTER_NAME"
    return
  fi

  log "Creating AKS cluster: $CLUSTER_NAME"

  local -a create_cmd=(
    az aks create
    --resource-group "$RESOURCE_GROUP"
    --name "$CLUSTER_NAME"
    --location "$LOCATION"
    --nodepool-name "$SYSTEM_POOL_NAME"
    --node-count "$SYSTEM_NODE_COUNT"
    --node-vm-size "$SYSTEM_NODE_VM_SIZE"
    --enable-managed-identity
    --enable-cluster-autoscaler
    --min-count 1
    --max-count 3
    --network-plugin azure
    --network-plugin-mode overlay
    --attach-acr "$ACR_NAME"
    --pod-cidr "$POD_CIDR"
    --service-cidr "$SERVICE_CIDR"
    --dns-service-ip "$DNS_SERVICE_IP"
    --generate-ssh-keys
  )

  if [[ -n "$K8S_VERSION" ]]; then
    create_cmd+=(--kubernetes-version "$K8S_VERSION")
  fi

  run_cmd "${create_cmd[@]}"
}

ensure_user_pool() {
  if node_pool_exists "$USER_POOL_NAME"; then
    log "User node pool already exists: $USER_POOL_NAME"
    return
  fi

  log "Adding user node pool: $USER_POOL_NAME"
  run_cmd az aks nodepool add \
    --resource-group "$RESOURCE_GROUP" \
    --cluster-name "$CLUSTER_NAME" \
    --name "$USER_POOL_NAME" \
    --mode User \
    --node-count "$USER_NODE_COUNT" \
    --node-vm-size "$USER_NODE_VM_SIZE" \
    --enable-cluster-autoscaler \
    --min-count 0 \
    --max-count 3
}

ensure_gpu_pool_if_enabled() {
  if [[ "$ENABLE_GPU_POOL" != "true" ]]; then
    log "GPU pool disabled (enable with --enable-gpu-pool)."
    return
  fi

  if node_pool_exists "$GPU_POOL_NAME"; then
    log "GPU node pool already exists: $GPU_POOL_NAME"
    return
  fi

  warn "Creating GPU node pool. Ensure quota is available for $GPU_NODE_VM_SIZE in $LOCATION."
  run_cmd az aks nodepool add \
    --resource-group "$RESOURCE_GROUP" \
    --cluster-name "$CLUSTER_NAME" \
    --name "$GPU_POOL_NAME" \
    --mode User \
    --node-count "$GPU_NODE_COUNT" \
    --min-count 0 \
    --max-count 1 \
    --node-vm-size "$GPU_NODE_VM_SIZE" \
    --enable-cluster-autoscaler \
    --labels gpu.sku="$GPU_NODE_VM_SIZE" \
    --node-taints gpu=true:NoSchedule
}

configure_kubeconfig() {
  if [[ "$SKIP_KUBECONFIG" == "true" ]]; then
    log "Skipping kubeconfig retrieval (--skip-kubeconfig)."
    return
  fi

  log "Fetching kubeconfig for cluster context"
  run_cmd az aks get-credentials \
    --resource-group "$RESOURCE_GROUP" \
    --name "$CLUSTER_NAME" \
    --overwrite-existing
}

validate_cluster_health() {
  if [[ "$DRY_RUN" == "true" || "$SKIP_KUBECONFIG" == "true" ]]; then
    log "Skipping health checks in dry-run/skip-kubeconfig mode."
    return
  fi

  log "Validating node readiness"
  kubectl get nodes -o wide

  log "Validating core system pods"
  kubectl -n kube-system get pods
}

main() {
  parse_args "$@"
  validate_prereqs

  log "Using subscription: $SUBSCRIPTION_ID"
  run_cmd az account set --subscription "$SUBSCRIPTION_ID"

  create_resource_group
  create_or_reuse_cluster
  ensure_user_pool
  ensure_gpu_pool_if_enabled
  configure_kubeconfig
  validate_cluster_health

  log "AKS bootstrap complete."
}

main "$@"
