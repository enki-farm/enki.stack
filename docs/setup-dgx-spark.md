# enki.stack DGX Spark + k3s Setup (Primary)

This guide installs the enki.stack "all-in-one AI box" baseline on a single
NVIDIA DGX Spark running **DGX OS**, using k3s as the Kubernetes distribution.

## Prerequisites

- DGX Spark running DGX OS (driver pre-installed)
- `kubectl`, `helm`, `kustomize` installed on the box
- Internet access from the box (Helm charts / container images are pulled online)

## 1) Install k3s

```bash
./infra/k3s/install-k3s.sh
```

Default behavior:

- Installs k3s in single-node `server` mode
- Disables the bundled Traefik (Envoy Gateway owns ingress instead)
- Keeps ServiceLB/klipper-lb so the Gateway's `LoadBalancer` Service gets a local IP
- Writes kubeconfig to `~/.kube/config`

```bash
# Validate commands without installing
./infra/k3s/install-k3s.sh --dry-run

# Add a LAN hostname as a TLS SAN
./infra/k3s/install-k3s.sh --tls-san dgx-spark.local
```

## 2) Install the NVIDIA GPU Operator

```bash
./infra/k3s/install-gpu-operator.sh
```

Uses `infra/k3s/values-gpu-operator.yaml`, which sets `driver.enabled=false`
since DGX OS already ships the NVIDIA driver — the operator only manages the
container toolkit, device plugin, and DCGM exporter.

Verify:

```bash
kubectl -n gpu-operator get pods
kubectl get nodes -o json | jq '.items[].status.allocatable."nvidia.com/gpu"'
```

## 3) Install Envoy Gateway + Envoy AI Gateway

```bash
./infra/gateway/install-envoy-gateway.sh
./infra/gateway/install-ai-gateway.sh
```

Envoy AI Gateway was renamed upstream to **Agent Router** (same CRDs/API
group/namespaces/chart names) — see https://github.com/envoyproxy/ai-gateway.
Re-check the current chart version at https://theagentrouter.ai/docs before
pinning `--chart-version` in automation.

## 4) Apply the DGX Spark kustomize overlay

The one-command flow below creates a random Grafana admin password and stores
it in the cluster as the `grafana-admin` Secret. For a manual apply, create
that Secret first and keep the generated value in a password manager:

```bash
kubectl create namespace observability --dry-run=client -o yaml | kubectl apply -f -
kubectl -n observability create secret generic grafana-admin \
  --from-literal=admin-password="$(openssl rand -base64 32 | tr -d '\n')"
```

```bash
kustomize build --load-restrictor=LoadRestrictionsNone k8s/overlays/dgx-spark | kubectl apply -f -
```

This applies:

- Shared namespaces from `k8s/base`
- cert-manager + a self-signed `ClusterIssuer` (no public domain needed) and a
  `Certificate` for the KServe ingress Gateway's HTTPS listener
- Gateway API CRDs + `GatewayClass`/`Gateway` (Envoy Gateway)
- KServe in **RawDeployment** mode (no Knative/Istio), fronted by Gateway API
- Grafana (basic deployment; no Prometheus/DCGM scraping wired up yet)
- Envoy AI Gateway routing CRs (`AIGatewayRoute`/`AIServiceBackend`) — placeholder
  wiring, adjust to your actual model backends

## 5) One-command flow

```bash
./scripts/deploy-dgx-spark.sh
```

## 6) Verify

```bash
kubectl get nodes -o wide
kubectl get gatewayclass envoy
kubectl -n kserve get gateway kserve-ingress-gateway
kubectl -n cert-manager get pods
kubectl -n kserve get pods
kubectl -n observability get pods
```

## 7) Deploy a sample model (HuggingFace pull-through)

Model storage for DGX Spark starts with HuggingFace Hub pull-through — KServe's
built-in `huggingfaceserver` runtime pulls `storageUri: hf://<org>/<repo>`
directly, no separate model registry required yet.

```bash
kubectl apply -f k8s/overlays/dgx-spark/examples/sample-inferenceservice.yaml
kubectl get inferenceservice -n ml-platform
```

Once `Ready`, send a request through the gateway's `LoadBalancer` IP with the
appropriate Host header (add an `/etc/hosts` entry, e.g. `models.spark.local`,
pointing at that IP).

## Roadmap / explicitly deferred

- **Kubeflow Model Registry** (`k8s/addons/kubeflow-model-registry`): still a
  placeholder addon; HuggingFace pull-through is the interim model source.
- **Auth**: no auth in front of Grafana/KServe endpoints yet (LAN-only,
  self-signed TLS). Planned: Envoy AI Gateway's built-in auth/rate-limiting.

## Upgrade policy

Same as the AKS path (see [setup-aks-kserve.md](setup-aks-kserve.md)): bump one
pinned upstream chart/URL at a time, apply, validate CRDs/webhooks/controller
health before the next bump.
