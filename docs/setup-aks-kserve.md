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

## 2) Install the monitoring stack

```bash
./infra/monitoring/install-monitoring.sh --platform aks
```

Three Helm releases in the `observability` namespace, all pinned in the script:
`kube-prometheus-stack` (prometheus-operator, Prometheus at 7d / 20Gi on
`managed-csi`, node-exporter, kube-state-metrics), `grafana`, and `tempo`
(single-binary, 72h retention on a 20Gi `managed-csi` PVC, OTLP only). It also
generates the `grafana-admin` Secret on first run.

Run this before step 4 — the overlay declares `ServiceMonitor`, `PodMonitor`
and `EnvoyProxy` resources whose CRDs this step installs — and before step 3,
since the AI Gateway exports spans to Tempo from startup.

## 3) Install Envoy Gateway + Envoy AI Gateway

```bash
./infra/gateway/install-ai-gateway.sh
```

`infra/gateway/ai-gateway-values.yaml` enables GenAI tracing: OTLP spans to
`tempo.observability.svc.cluster.local:4317` using the OpenTelemetry GenAI
semantic conventions, with full prompt/response content captured. Conversations
are grouped by `session.id`, taken from the `agent-session-id` request header;
see the **GenAI Conversations** and **AI Gateway Overview** dashboards.

## 4) Install KServe with kustomize

```bash
kustomize build --load-restrictor=LoadRestrictionsNone k8s/overlays/aks | kubectl apply -f -
```

This applies:

- Shared namespaces from `k8s/base`
- Cert-manager and KServe manifests from pinned upstream release URLs
- KServe in RawDeployment mode (no Knative/Istio) fronted by Gateway API/Envoy AI Gateway
- Monitoring content from `k8s/monitoring`: Grafana datasource, vendored
  dashboards and scrape targets

Check rollout status:

```bash
kubectl -n cert-manager get pods
kubectl -n kserve get pods
kubectl -n observability get pods
```

## 5) One-command flow

```bash
./scripts/deploy-aks.sh
```

## 6) Addons (scaffolded)

Scaffolded addon modules are available at:

- `k8s/addons/kubeflow-model-registry`

To include/exclude an addon, comment/uncomment it in `k8s/overlays/aks/kustomization.yaml`.
Monitoring is not an addon — it is always applied via `k8s/monitoring`.

## 7) GPU node pool

Enable a GPU node pool via `--enable-gpu-pool` on `create-aks.sh`, then
uncomment `../../aks` (the `k8s/aks/gpu-device-plugin.yaml` DaemonSet) in
`k8s/overlays/aks/kustomization.yaml`.

## 7) Upgrade policy

When upgrading KServe/cert-manager:

1. Bump one pinned upstream URL at a time.
2. Apply in dev overlay.
3. Validate CRDs, webhooks, and controller health before the next bump.
