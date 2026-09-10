# enki.infer AKS + KServe Setup (Production / larger deployments)

This guide installs an AKS cluster and applies a kustomize-based KServe
baseline. For a single-box setup, see [setup-dgx-spark.md](setup-dgx-spark.md)
(the primary quick-start target for enki.stack).

## Prerequisites

- Azure CLI logged in (`az login`)
- `kubectl`
- `kustomize`
- Permissions to create AKS and resource groups in the Azure subscription selected for deployment

## 1) Create AKS

From repo root:

```bash
./infra/aks/create-aks.sh
```

Default behavior:

- Subscription: configured by `--subscription-id` (the script default is a repository-specific example)
- Resource group: `rg-enki-stack`
- Cluster: `aks-enki-stack`
- Region: `westeurope`
- Network plugin: Azure CNI Overlay
- Node pools: system + user (`Standard_D4s_v5`)
- GPU pool: disabled by default

Useful examples:

```bash
# Validate commands without creating resources
./infra/aks/create-aks.sh --dry-run

# Change region
./infra/aks/create-aks.sh --location westeurope

# Enable GPU pool
./infra/aks/create-aks.sh --enable-gpu-pool
```

## 2) Install Envoy Gateway + Envoy AI Gateway

```bash
./infra/gateway/install-envoy-gateway.sh
./infra/gateway/install-ai-gateway.sh
```

## 3) Install KServe with kustomize

The one-command flow creates a random Grafana admin password and stores it in
the cluster as the `grafana-admin` Secret. For a manual apply, create that
Secret first using the same command shown in the DGX Spark guide.

```bash
kustomize build --load-restrictor=LoadRestrictionsNone k8s/overlays/aks | kubectl apply -f -
```

This applies:

- Shared namespaces from `k8s/base`
- Cert-manager and KServe manifests from pinned upstream release URLs
- KServe in RawDeployment mode (no Knative/Istio) fronted by Gateway API/Envoy AI Gateway

Check rollout status:

```bash
kubectl -n cert-manager get pods
kubectl -n kserve get pods
```

## 4) One-command flow

```bash
./scripts/deploy-aks.sh
```

## 5) Addons (scaffolded)

Scaffolded addon modules are available at:

- `k8s/addons/envoy-ai-gateway` (enabled by default in `k8s/overlays/aks`)
- `k8s/addons/kubeflow-model-registry`
- `k8s/addons/grafana` (enabled by default in `k8s/overlays/aks`)

To include/exclude an addon, comment/uncomment it in `k8s/overlays/aks/kustomization.yaml`.

## 6) GPU node pool

Enable a GPU node pool via `--enable-gpu-pool` on `create-aks.sh`, then
uncomment `../../aks` (the `k8s/aks/gpu-device-plugin.yaml` DaemonSet) in
`k8s/overlays/aks/kustomization.yaml`.

## 7) Upgrade policy

When upgrading KServe/cert-manager:

1. Bump one pinned upstream URL at a time.
2. Apply in dev overlay.
3. Validate CRDs, webhooks, and controller health before the next bump.
