# enki.stack DGX Spark + k3s Setup (Primary)

Install on an existing Kubernetes cluster on a single NVIDIA DGX Spark running
DGX OS. The CLI can run remotely with cluster credentials; it neither provisions
k3s nor changes kubeconfig. See [the CLI guide](cli.md) for configuration,
certificate solvers, ownership and recovery.

## Prerequisites

- Python 3.12+, Helm 3/4, kubectl; standalone Kustomize is optional.
- Ready cluster nodes and permissions for cluster-scoped controllers/CRDs.
- DGX OS's pre-installed NVIDIA driver for operator-managed GPUs.
- `local-path` storage for persistent components and LoadBalancer support.
- DNS/network access to the shared HTTPS gateway, and chart/image network access.

Provisioning belongs in the separate
[infrastructure guide](../enki.infra/docs/setup-dgx-spark.md).
Replace this staging link with the infrastructure repository URL after extraction.

## Configure and install

```bash
python -m pip install -e .
enkictl init -p profiles/spark/profile.yaml
enkictl validate -p profiles/spark/profile.yaml --render
enkictl install -p profiles/spark/profile.yaml --dry-run
enkictl install -p profiles/spark/profile.yaml
```

Select platform `dgx-spark`. GPU mode `operator` uses the pinned NVIDIA operator
with `driver.enabled=false`, managing the toolkit/device plugin/DCGM rather than
replacing DGX OS's driver. Select `existing` to leave GPU management external.

Full-core installation includes certificates, AI Gateway, Dex, monitoring,
optional GPU Operator, KServe and [standalone Hub](kubeflow-hub.md). It does not
deploy models. Top-level install also installs any optional addons selected
during init, after their component prerequisites. Do not apply a legacy whole-stack overlay on top:
it bypasses CLI ownership and may include model resources.

## Install or remove components separately

```bash
enkictl init --component gpu-operator -p profiles/gpu/profile.yaml
enkictl component install gpu-operator -p profiles/gpu/profile.yaml --dry-run
enkictl component install gpu-operator -p profiles/gpu/profile.yaml
enkictl component install auth kserve -p profiles/spark/profile.yaml
enkictl component install monitoring -p profiles/spark/profile.yaml
enkictl component list -p profiles/spark/profile.yaml
enkictl component uninstall kserve -p profiles/spark/profile.yaml --dry-run
```

Use the same profile for components belonging to the same installation. The
separate GPU-only profile above illustrates a standalone installation, not a way
to share ownership between profiles.

Missing required components are included automatically. Installed dependencies
are verified, not silently upgraded. Named removal leaves dependencies in place
and refuses to remove something still needed by another managed component.

GPU Operator can install without monitoring. Monitoring owns the DCGM scrape
configuration, so Prometheus CRDs are not needed by the operator. AI Gateway
can also install without monitoring, with a warning that traces cannot reach
Tempo until monitoring is installed.

## Verify

```bash
kubectl --context spark get nodes -o wide
kubectl --context spark -n gpu-operator get pods
kubectl --context spark -n envoy-ai-gateway-system get gateway
kubectl --context spark -n cert-manager get pods
kubectl --context spark -n kserve get pods
kubectl --context spark -n observability get pods
```

Use your profile's context rather than `spark` if different. Check certificate
issuance, browser login/group denial, GPU readiness and scrape targets on the
actual machine. Rendering tests are not host-driver or live-cluster certification.

The AI Gateway exports OpenTelemetry GenAI spans to Tempo. Its checked-in values
capture full prompts/responses; review
[the values](../helm/gateway/ai-gateway-values.yaml) before production use.
Conversations use `session.id` from the `agent-session-id` header.

## Models and upgrades

Install [the default model](../models/default/README.md) separately, adapting its
hostnames/storage to your deployment. Model routes and machine-authentication
policies are not installed by the core CLI. Usage rate-limiting remains manual
and outside component installation; see
[the limiter notes](../k8s/addons/ai-gateway-ratelimit/README.md).

Pins live in [helm/platform.yaml](../helm/platform.yaml). Bump one upstream chart
or vendored API at a time and verify CRDs, webhooks and controller readiness on a
disposable cluster before upgrading production. Fresh installations only are
supported; neither old shell resources nor prior CLI inventories are adopted.
