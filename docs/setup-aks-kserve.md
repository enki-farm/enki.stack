# enki.stack AKS + KServe Setup (Production / larger deployments)

This guide installs the stack on an existing AKS cluster and applies a KServe
baseline. For a single-box setup, see [setup-dgx-spark.md](setup-dgx-spark.md)
(the primary quick-start target for enki.stack).

## Prerequisites

- Running AKS cluster with Ready nodes and working current-context credentials
- `kubectl`, `helm`, `openssl`; `kustomize` or `kubectl kustomize`
- Permissions to install cluster-scoped CRDs and controllers
- `managed-csi` storage, LoadBalancer support and network access for charts/images
- GPU nodes and driver/device-plugin support for GPU inference

## 1) Check cluster access

```bash
kubectl config current-context
kubectl get nodes -o wide
kubectl get storageclass
```

Provisioning belongs in the separate [AKS infrastructure guide](../enki.infra/docs/setup-aks.md).
Replace this staging link with the final infrastructure repository URL after
extraction. Stack deployment does not invoke Azure CLI or retrieve credentials.

## 2) Install the monitoring stack

```bash
./scripts/install-monitoring.sh --platform aks
```

Three Helm releases in the `observability` namespace, all pinned in the script:
`kube-prometheus-stack` (prometheus-operator, Prometheus at 7d / 20Gi on
`managed-csi`, node-exporter, kube-state-metrics), `grafana`, and `tempo`
(single-binary, 72h retention on a 20Gi `managed-csi` PVC, OTLP only). It also
generates the `grafana-admin` Secret on first run.

Run this before step 4: the overlay declares `ServiceMonitor` and `PodMonitor`
resources whose CRDs this step installs. Also run it before step 3,
since the AI Gateway exports spans to Tempo from startup.

Monitoring installs ServiceMonitor/PodMonitor CRDs; the gateway step installs
EnvoyProxy CRDs.

## 3) Install Envoy Gateway + Envoy AI Gateway

```bash
./scripts/install-ai-gateway.sh
```

`helm/gateway/ai-gateway-values.yaml` enables GenAI tracing: OTLP spans to
`tempo.observability.svc.cluster.local:4317` using the OpenTelemetry GenAI
semantic conventions, with full prompt/response content captured. Conversations
are grouped by `session.id`, taken from the `agent-session-id` request header;
see the **GenAI Conversations** and **AI Gateway Overview** dashboards.

## 4) Install KServe and apply the overlay

```bash
./scripts/install-kserve.sh
kustomize build --load-restrictor=LoadRestrictionsNone k8s/overlays/aks | kubectl apply -f -
```

This applies:

- Shared namespaces from `k8s/base`
- Vendored cert-manager deployments and Gateway API CRDs
- KServe model resources in Standard mode (no Knative/Istio) fronted by Gateway API/Envoy AI Gateway
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
./scripts/deploy.sh --platform aks
```

This uses the current Kubernetes context without changing kubeconfig. There
are no cluster-provisioning or dry-run options on this command.

## 6) Addons (scaffolded)

Scaffolded addon modules are available at:

- `k8s/addons/kubeflow-model-registry`

To include/exclude an addon, comment/uncomment it in `k8s/overlays/aks/kustomization.yaml`.
Monitoring is not an addon — it is always applied via `k8s/monitoring`.

## 7) GPU node pool

Ask the infrastructure administrator to provide GPU nodes, then, if no device
plugin is already present, uncomment `../../aks` (the
`k8s/aks/gpu-device-plugin.yaml` DaemonSet) in
`k8s/overlays/aks/kustomization.yaml`.

## 8) Upgrade policy

When upgrading KServe/cert-manager:

1. Bump one pinned upstream URL at a time.
2. Apply in dev overlay.
3. Validate CRDs, webhooks, and controller health before the next bump.
