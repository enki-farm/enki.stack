# enki.stack

Open-source AI stack baseline. Primary target: a single **NVIDIA DGX Spark**
running **DGX OS** + k3s as an all-in-one AI box. AKS remains supported for
production / larger, multi-node deployments.

Licensed under the [Apache License 2.0](LICENSE).

## Current implementation

- k3s bootstrap: `infra/k3s/install-k3s.sh`
- NVIDIA GPU Operator bootstrap (DGX OS driver, operator-managed toolkit/device-plugin/DCGM): `infra/gpu-operator/install-gpu-operator.sh`
- Envoy Gateway + Envoy AI Gateway (aka Agent Router) bootstrap: `infra/gateway/`
- KServe + LLMInferenceService install via Helm (Standard mode, no Knative/Istio) using `infra/kserve/install-kserve.sh`
- Monitoring (Prometheus + Grafana, node-exporter, kube-state-metrics, DCGM): `infra/monitoring/install-monitoring.sh` + `k8s/monitoring`
- Addons enabled by default: Envoy AI Gateway routing
- Addon scaffold (placeholder): Kubeflow Model Registry
- AKS bootstrap script (production path): `infra/aks/create-aks.sh`, overlay `k8s/overlays/aks`

## Prerequisites (DGX Spark)

- NVIDIA DGX Spark running DGX OS, with the NVIDIA driver pre-installed
- Internet access for downloading k3s, Helm charts, and container images
- `kubectl`, `helm`, `kustomize`, `curl`, and `openssl` available on `PATH`
- Sufficient privileges to install k3s and write `/etc/rancher/k3s`

## Quick start (DGX Spark)

```bash
./scripts/deploy-dgx-spark.sh
```

For detailed setup steps, see [docs/setup-dgx-spark.md](docs/setup-dgx-spark.md).

## Production / larger deployments (AKS)

```bash
./scripts/deploy-aks.sh
```

For detailed setup steps, see [docs/setup-aks-kserve.md](docs/setup-aks-kserve.md).

## Roadmap

- Kubeflow Model Registry: real install, replacing HuggingFace-pull-through-only model storage.
- Envoy AI Gateway client authentication and rate limiting (the public hostname
	routing is configured; authentication still requires a cluster-specific
	credential policy).

## Manual install reference

The commands below are what `scripts/deploy-dgx-spark.sh` / `scripts/deploy-aks.sh`
automate; useful for debugging a single step.

### Install cert-manager

```bash
kubectl apply -f k8s/cert-manager/deployment.yaml
```

### Install Gateway API + Envoy Gateway / Envoy AI Gateway

```bash
kubectl apply --server-side -f k8s/gateway-api/deployment.yaml
./infra/gateway/install-ai-gateway.sh
kubectl apply -f k8s/gateway-api/gatewayclass.yaml
kubectl apply -f k8s/gateway-api/gateway.yaml
```

### Install KServe

```bash
./infra/kserve/install-kserve.sh
```

`infra/kserve/` keeps full local copies of the Helm values used for KServe,
LLMInferenceService, and runtime configs. The checked-in values set Standard mode
and disable KServe-managed ingress creation; external model traffic is exposed by
Envoy AI Gateway and forwarded to cluster-local predictor services.

### Install monitoring

```bash
./infra/monitoring/install-monitoring.sh --platform dgx-spark
kubectl apply -k k8s/monitoring
```

The Helm step must run first: it installs the prometheus-operator CRDs that
`k8s/monitoring` depends on. To iterate on dashboards alone:

```bash
./infra/monitoring/install-monitoring.sh --platform dgx-spark --dashboards-only
```

#TODO add tolerations to nvidia gpu plugin. Should not run on CPU only nodes (AKS path only; DGX Spark uses the GPU Operator instead).
