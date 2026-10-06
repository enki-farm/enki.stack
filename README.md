# enki.stack

Open-source AI stack baseline. Primary target: a single **NVIDIA DGX Spark**
running **DGX OS** with an existing Kubernetes cluster as an all-in-one AI box.
AKS remains supported for production / larger, multi-node deployments.

Licensed under the [Apache License 2.0](LICENSE).

## Current implementation

- NVIDIA GPU Operator bootstrap (DGX OS driver, operator-managed toolkit/device-plugin/DCGM): `scripts/install-gpu-operator.sh`
- Envoy Gateway + Envoy AI Gateway (aka Agent Router) bootstrap: `helm/gateway/`
- KServe + LLMInferenceService install via Helm (Standard mode, no Knative/Istio) using `scripts/install-kserve.sh`
- Monitoring (Prometheus + Grafana, node-exporter, kube-state-metrics, DCGM): `scripts/install-monitoring.sh` + `k8s/monitoring`
- Addons enabled by default: Envoy AI Gateway routing
- Addon scaffold (placeholder): Kubeflow Model Registry
- AKS stack profile: `k8s/overlays/aks`

Cluster provisioning and Cloudflared are separate from the stack. During
extraction, their standalone repository is staged in [enki.infra](enki.infra/README.md).
Replace these local documentation links with the final repository URL when moving it out.

## Prerequisites

- A running Kubernetes cluster with Ready nodes and a reachable API
- Working kubeconfig/current context and permissions to install cluster-scoped CRDs/controllers
- `kubectl`, `helm`, and `openssl` on PATH; standalone `kustomize` is preferred, with `kubectl kustomize` as fallback
- Network access for Helm charts and container images
- Storage and LoadBalancer support matching the selected profile: `local-path` for DGX Spark, `managed-csi` for AKS
- Compatible GPU nodes/host drivers for inference; DGX Spark uses the pre-installed DGX OS driver

Deployment uses your current context and does not modify kubeconfig. It can run
remotely; neither root access to the host nor Azure CLI is required. Existing
controllers using the same namespaces, releases or CRDs need review before install.

## Quick start (DGX Spark)

```bash
./scripts/deploy.sh --platform dgx-spark
```

For a cluster with existing GPU management, add `--skip-gpu-operator`.
The deployment command has no dry-run or cluster-provisioning options.

For detailed setup steps, see [docs/setup-dgx-spark.md](docs/setup-dgx-spark.md).

## Production / larger deployments (AKS)

```bash
./scripts/deploy.sh --platform aks
```

For detailed setup steps, see [docs/setup-aks-kserve.md](docs/setup-aks-kserve.md).

## Roadmap

- Kubeflow Model Registry: real install, replacing HuggingFace-pull-through-only model storage.
- Envoy AI Gateway client authentication and rate limiting (the public hostname
	routing is configured; authentication still requires a cluster-specific
	credential policy).

## Manual install reference

The commands below are what `scripts/deploy.sh --platform dgx-spark` / `scripts/deploy.sh --platform aks`
automate; useful for debugging a single step.

### Install cert-manager

```bash
kubectl apply -f k8s/cert-manager/deployment.yaml
```

### Install Gateway API + Envoy Gateway / Envoy AI Gateway

```bash
kubectl apply --server-side -f k8s/gateway-api/deployment.yaml
./scripts/install-ai-gateway.sh
kubectl apply -f k8s/gateway-api/gatewayclass.yaml
kubectl apply -f k8s/gateway-api/gateway.yaml
```

### Install KServe

```bash
./scripts/install-kserve.sh
```

`helm/kserve/` keeps full local copies of the Helm values used for KServe,
LLMInferenceService, and runtime configs. The checked-in values set Standard mode
and disable KServe-managed ingress creation; external model traffic is exposed by
Envoy AI Gateway and forwarded to cluster-local predictor services.

### Install monitoring

```bash
./scripts/install-monitoring.sh --platform dgx-spark
kubectl apply -k k8s/monitoring
```

The Helm step must run first: it installs the prometheus-operator CRDs that
`k8s/monitoring` depends on. To iterate on dashboards alone:

```bash
./scripts/install-monitoring.sh --platform dgx-spark --dashboards-only
```

#TODO add tolerations to nvidia gpu plugin. Should not run on CPU only nodes (AKS path only; DGX Spark uses the GPU Operator instead).
