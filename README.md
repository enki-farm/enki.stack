# enki.stack

Open-source AI stack baseline. Primary target: a single **NVIDIA DGX Spark**
running **DGX OS** with an existing Kubernetes cluster as an all-in-one AI box.
AKS remains supported for production / larger, multi-node deployments.

Licensed under the [Apache License 2.0](LICENSE).

## Features

enki.stack is an open-source AI stack baseline that delivers a production-ready, Kubernetes-native platform for AI model serving, management, and consumption. It targets a single NVIDIA DGX Spark as an all-in-one AI box, with AKS support for larger multi-node deployments.

- **AI Gateway** — Envoy AI Gateway (Agent Router) with rate limiting, session grouping, and OpenTelemetry GenAI tracing for full prompt/response observability
- **Model Serving** — KServe LLMInferenceService with vLLM runtime, HuggingFace pull-through, and direct OCI/Zot artifact storage
- **Model Registry & Catalog** — Standalone Kubeflow Hub with MySQL-backed Model Registry, PostgreSQL Model Catalog, and authenticated Zot OCI registry
- **Authentication & Identity** — Dex OIDC issuer with group-based access control, per-app OIDC overrides, and HTTPS-only ingress
- **Observability** — Prometheus, Grafana, Tempo (OTLP tracing), DCGM exporter, and dedicated GenAI conversation dashboards
- **GPU Management** — NVIDIA GPU Operator managing toolkit, device plugin, and DCGM exporter on DGX OS
- **Chat & RAG** — LibreChat with Crawl4AI integration, CPU-only embedding service for RAG, and optional admin panel
- **Security** — Nemo Guardrails, usage-based rate limiting, network policies, and authenticated OCI storage
- **Multi-platform Deployment** — Single-command deployment for DGX Spark (k3s) and AKS profiles with platform-specific overlays

## Current implementation

- NVIDIA GPU Operator bootstrap (DGX OS driver, operator-managed toolkit/device-plugin/DCGM): `scripts/install-gpu-operator.sh`
- Envoy Gateway + Envoy AI Gateway (aka Agent Router) bootstrap: `helm/gateway/`
- KServe + LLMInferenceService install via Helm (Standard mode, no Knative/Istio) using `scripts/install-kserve.sh`
- Monitoring (Prometheus + Grafana, node-exporter, kube-state-metrics, DCGM): `scripts/install-monitoring.sh` + `k8s/monitoring`
- Core Dex identity foundation in [k8s/dex](k8s/dex), with native app OIDC overrides: [authentication setup](docs/authentication.md). Model authentication remains blocked pending token compatibility.
- Gateway HTTPS is the only supported ingress path. Configure a TLS Secret for the shared Gateway; for public `zer0.garden` hosts, use [Cloudflare/Azure DNS-01 certificates](k8s/certificates/README.md).
- Addons enabled by default: Envoy AI Gateway routing
- Core standalone Kubeflow Hub with Model Registry, MySQL, Hugging Face Model Catalog with PostgreSQL, authenticated Zot OCI storage, and KServe integration: [Hub setup](docs/kubeflow-hub.md).
- AKS stack profile: `k8s/overlays/aks`

Cluster provisioning and Cloudflared are separate from the stack. During
extraction, their standalone repository is staged in [enki.infra](enki.infra/README.md).
Replace these local documentation links with the final repository URL when moving it out.

## Prerequisites

- A running Kubernetes cluster with Ready nodes and a reachable API
- Working kubeconfig/current context and permissions to install cluster-scoped CRDs/controllers
- `kubectl`, `helm`, `openssl`, and `jq` on PATH; standalone `kustomize` is preferred, with `kubectl kustomize` as fallback
- Network access for Helm charts and container images
- Storage and LoadBalancer support matching the selected profile: `local-path` for DGX Spark, `managed-csi` for AKS
- Compatible GPU nodes/host drivers for inference; DGX Spark uses the pre-installed DGX OS driver
- Configured Dex identity provider and HTTPS app origins: follow [authentication setup](docs/authentication.md) before deployment

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

- Hub: database/OCI backups, Zot retention and large-model ingress; see [later improvements](docs/kubeflow-hub.md#later-improvements). Existing Hugging Face caches remain unchanged.
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

The gateway installer pins AgentRouter v1.2.0, Envoy Gateway v1.9.2 and the
security-patched Envoy Proxy v1.39.3. It updates Gateway API v1.6.1 experimental
CRDs and Envoy extension CRDs before upgrading controllers. For an opt-in
authenticated usage-limit example on DGX Spark, see
[models/default-rate-limited/README.md](models/default-rate-limited/README.md).
Existing model access is unchanged.

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
bash scripts/install-auth.sh
./scripts/install-monitoring.sh --platform dgx-spark --auth-dir .generated/auth
kubectl apply -k k8s/monitoring
```

Dex is a core component installed after Envoy Gateway and before monitoring.
Its installer provisions configuration and Secrets, so do not apply
[k8s/dex](k8s/dex) alone. LibreChat remains an optional app; pass
`--auth-dir .generated/auth` to its installer when enabling it.

The Helm step must run first: it installs the prometheus-operator CRDs that
`k8s/monitoring` depends on. To iterate on dashboards alone:

```bash
./scripts/install-monitoring.sh --platform dgx-spark --dashboards-only
```

#TODO add tolerations to nvidia gpu plugin. Should not run on CPU only nodes (AKS path only; DGX Spark uses the GPU Operator instead).

## Roadmap

### Prio 1
- deepeval jobs for evaluation -> store raw json reports
- finish Hub browser authentication acceptance and production artifact-storage hardening
- hardening & documentation !

### Prio 2
- vllm semantic router
- librechat scheduled agents
- librechat self-hosted code interpreter