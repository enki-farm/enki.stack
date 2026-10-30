# enki.stack

> **Work in progress:** enki.stack is being built in public and has not yet
> reached a stable release candidate. Features, interfaces, and deployment
> workflows may change substantially.

Open-source AI stack baseline. Primary target: a single **NVIDIA DGX Spark**
running **DGX OS** with an existing Kubernetes cluster as an all-in-one AI box.
AKS remains supported for production / larger, multi-node deployments.

Licensed under the [Apache License 2.0](LICENSE).

The repository's declared dependency inventory is available as a
[CycloneDX SBOM](sbom.cdx.json). It records direct Python requirements and
pinned core Helm charts; it is not a resolved SBOM for a deployed release.

## Features

enki.stack is an open-source AI stack baseline that delivers a production-ready, Kubernetes-native platform for AI model serving, management, and consumption. It targets a single NVIDIA DGX Spark as an all-in-one AI box, with AKS support for larger multi-node deployments.

- **AI Gateway** — Envoy AI Gateway (Agent Router) with rate limiting, session grouping, and OpenTelemetry GenAI tracing for full prompt/response observability
- **Model Serving** — KServe LLMInferenceService with vLLM runtime, HuggingFace pull-through, and direct OCI/Zot artifact storage
- **Model Registry & Catalog** — Standalone Kubeflow Hub with MySQL-backed Model Registry, PostgreSQL Model Catalog, and authenticated Zot OCI registry
- **Authentication & Identity** — Dex OIDC issuer with group-based access control, per-app OIDC overrides, and HTTPS-only ingress
- **Observability** — Prometheus, Grafana, Tempo (OTLP tracing), DCGM exporter, and dedicated GenAI conversation dashboards
- **GPU Management** — NVIDIA GPU Operator managing toolkit, device plugin, and DCGM exporter on DGX OS
- **Chat & RAG** — Optional LibreChat addon with Crawl4AI integration and CPU-only embeddings
- **Security** — Nemo Guardrails, usage-based rate limiting, network policies, and authenticated OCI storage
- **Multi-platform Deployment** — Single-command deployment for DGX Spark (k3s) and AKS profiles with platform-specific overlays

## Current implementation

- NVIDIA GPU Operator bootstrap (DGX OS driver, operator-managed toolkit/device-plugin/DCGM): `enkictl component install gpu-operator`
- Envoy Gateway + Envoy AI Gateway (aka Agent Router) bootstrap: `helm/gateway/`
- KServe + LLMInferenceService install via Helm (Standard mode, no Knative/Istio): `enkictl component install kserve`
- Monitoring (Prometheus + Grafana, node-exporter, kube-state-metrics, DCGM): `enkictl component install monitoring` + `k8s/monitoring`
- Core Dex identity foundation in [k8s/dex](k8s/dex), with native app OIDC overrides: [authentication setup](docs/authentication.md). Model authentication remains blocked pending token compatibility.
- Gateway HTTPS is the only supported ingress path. Configure a TLS Secret for the shared Gateway; for public `your.domain` hosts, use [Cloudflare/Azure DNS-01 certificates](k8s/certificates/README.md).
- Addons enabled by default: Envoy AI Gateway routing
- Core standalone Kubeflow Hub with Model Registry, MySQL, Hugging Face Model Catalog with PostgreSQL, authenticated Zot OCI storage, and KServe integration: [Hub setup](docs/kubeflow-hub.md).
- AKS stack profile: `k8s/overlays/aks`

Cluster provisioning and Cloudflared are separate from the stack.

## Prerequisites

For the profile-driven Python CLI, see [CLI setup and lifecycle](docs/cli.md).
It configures Dex and cert-manager (Azure DNS, Cloudflare, or self-signed) and
calls Helm/Kustomize directly through independently selectable component modules.
The superseded core shell installers have been removed.

- A running Kubernetes cluster with Ready nodes and a reachable API
- Working kubeconfig/current context and permissions to install cluster-scoped CRDs/controllers
- Python 3.12+, `kubectl` and Helm 3/4; standalone `kustomize` is optional, with `kubectl kustomize` as fallback
- Network access for Helm charts and container images
- Storage and LoadBalancer support matching the selected profile: `local-path` for DGX Spark, `managed-csi` for AKS
- Compatible GPU nodes/host drivers for inference; DGX Spark uses the pre-installed DGX OS driver
- Configured Dex identity provider and HTTPS app origins: follow [authentication setup](docs/authentication.md) before deployment

Deployment uses the profile's explicit context and does not modify kubeconfig. It can run
remotely; neither root access to the host nor Azure CLI is required. Existing
controllers using the same namespaces, releases or CRDs need review before install.

## Quick start (DGX Spark)

For a fresh CLI-owned platform:

```bash
python -m pip install -e .
enkictl init --profile profiles/spark/profile.yaml
enkictl validate --profile profiles/spark/profile.yaml
enkictl install --profile profiles/spark/profile.yaml --dry-run
enkictl install --profile profiles/spark/profile.yaml
```

The recommended setup is to install all components supported by your platform;
the init wizard selects them all by default. Addons remain opt-in. Top-level
`install` installs the profile's saved component selection followed by its
selected addons. Top-level `uninstall` removes all recorded addons and components
for that profile, preserving data unless `--purge` is requested.

See the [CLI guide](docs/cli.md) for profiles, local secrets, certificate solvers,
preflight, safe uninstall/purge and AKS. Models are installed separately; optional
apps can be selected during init or installed individually. Existing
bash-installed resources are not automatically adopted.

Install or remove individual components with the same profile:

```bash
enkictl component install auth kserve -p profiles/spark/profile.yaml
enkictl component list -p profiles/spark/profile.yaml
enkictl component uninstall kserve -p profiles/spark/profile.yaml --dry-run
```

Missing dependencies are included and previewed. Named removal leaves dependencies
installed and refuses removal while managed dependents remain. For existing GPU
management, set `gpu: existing`. Existing generated JSON inventories are migrated
to the profile's `inventory.yaml` on the next write.

For detailed setup steps, see [docs/setup-dgx-spark.md](docs/setup-dgx-spark.md).

## Production / larger deployments (AKS)

```bash
enkictl init -p profiles/aks/profile.yaml
enkictl install -p profiles/aks/profile.yaml --dry-run
enkictl install -p profiles/aks/profile.yaml
```

For detailed setup steps, see [docs/setup-aks-kserve.md](docs/setup-aks-kserve.md).

See the [project roadmap](ROADMAP.md) for planned work and priorities.

## Manual install reference

Use named components to operate individual parts of the stack while retaining
ownership, recovery and dependency safeguards. Do not mix direct whole-stack
overlay application with a CLI installation.

### Install cert-manager

```bash
enkictl component install certificates -p profiles/spark/profile.yaml
```

### Install Gateway API + Envoy Gateway / Envoy AI Gateway

```bash
enkictl component install ai-gateway -p profiles/spark/profile.yaml
```

The component pins AgentRouter v1.2.0, Envoy Gateway v1.9.2 and the
security-patched Envoy Proxy v1.39.3. It updates Gateway API v1.6.1 experimental
CRDs and Envoy extension CRDs before upgrading controllers. For an opt-in
usage-limit assets, see [the limiter notes](k8s/addons/ai-gateway-ratelimit/README.md).
Rate-limiting is not configured by the core component installer.

### Install KServe

```bash
enkictl component install kserve -p profiles/spark/profile.yaml
```

`helm/kserve/` keeps full local copies of the Helm values used for KServe,
LLMInferenceService, and runtime configs. The checked-in values set Standard mode
and disable KServe-managed ingress creation; external model traffic is exposed by
Envoy AI Gateway and forwarded to cluster-local predictor services.

### Install monitoring

```bash
enkictl component install monitoring -p profiles/spark/profile.yaml
```

Dex is a dependency installed after Envoy Gateway and before monitoring.
Its component provisions configuration and Secrets, so do not apply
[k8s/dex](k8s/dex) alone. LibreChat remains a separate optional addon, managed
with `enkictl addon install librechat`. It uses Dex-only SSO with
`auth.chat_group` (defaulting to `auth.hub_group`). Reconcile the `auth` component
before upgrading older CLI installations to register the LibreChat client.

The component installs prometheus-operator CRDs before monitoring content.
To reconcile dashboards, rerun monitoring (this also reconciles its releases):

```bash
enkictl component install monitoring -p profiles/spark/profile.yaml
```
