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

### Node-exporter scrapes fail with HTTP 503

On DGX Spark, the node-exporter `cpufreq` collector can block indefinitely while
reading CPU-frequency sysfs files. In the observed failure (node-exporter 1.12.1),
40 scrapes accumulated over several days, exhausting the default concurrent
request limit. `/metrics` then returned HTTP 503 with
`Limit of concurrent requests reached (40), try again later.` even though the
pod remained Running and Ready: its probes check `/`, not collection.
Prometheus marked the target down and metrics such as `node_cpu_seconds_total`
became unavailable.

The exporter's `/debug/pprof/goroutine?debug=2` showed the blocked requests waiting
in `cpuFreqCollector.Update`, `SystemCpufreq`, and `os.ReadFile`. Individual
collectors could still respond, including new `cpufreq` requests; a successful
one-off test therefore does not rule out older stuck reads. Three scrapes with
`/metrics?exclude[]=cpufreq` succeeded in under 0.25 seconds each and included
`node_cpu_seconds_total`. The underlying kernel/driver cause was not established.

The [DGX Spark monitoring values](../helm/monitoring/values-kube-prometheus-stack-dgx-spark.yaml)
disable only `cpufreq` with `--no-collector.cpufreq`, preserving the chart's
filesystem exclusions. CPU-frequency metrics are unavailable; CPU-time and
usage metrics remain enabled. The AKS configuration is unchanged.

Reconcile monitoring to apply the setting and roll node-exporter, clearing the
stuck requests:

```bash
enkictl component install monitoring -p profiles/spark/profile.yaml
kubectl --context spark -n observability rollout status \
  daemonset/kube-prometheus-stack-prometheus-node-exporter
```

Verify that Prometheus reports `up{job="node-exporter"} == 1` and that the raw
`node_cpu_seconds_total` query returns series. Raising `--web.max-requests` only
delays saturation; restarting without disabling the collector can recur.

## Models and upgrades

Install [the default model](../models/default/README.md) separately, adapting its
hostnames/storage to your deployment. Model routes and the implemented
[machine-access policy](authentication.md#machine-access-gateway-api-keys)
are installed separately from the core CLI with `kubectl apply -k models`
and the selected model packages. Usage rate-limiting remains manual
and outside component installation; see
[the limiter notes](../k8s/addons/ai-gateway-ratelimit/README.md).

Pins live in [helm/platform.yaml](../helm/platform.yaml). Bump one upstream chart
or vendored API at a time and verify CRDs, webhooks and controller readiness on a
disposable cluster before upgrading production. Fresh installations only are
supported; neither old shell resources nor prior CLI inventories are adopted.
