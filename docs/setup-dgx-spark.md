# enki.stack DGX Spark + k3s Setup (Primary)

This guide installs enki.stack on an existing Kubernetes cluster on a single
NVIDIA DGX Spark running **DGX OS**. Deployment can run from any machine with
working cluster credentials; it does not provision k3s or change kubeconfig.

## Prerequisites

- Running Kubernetes with Ready nodes on DGX OS (NVIDIA driver pre-installed)
- Working current context with permissions to install cluster-scoped resources
- `kubectl`, `helm`, `openssl`; `kustomize` or `kubectl kustomize`
- `local-path` storage and LoadBalancer support, without conflicting ingress bindings
- Internet access for Helm charts and container images

## 1) Check cluster access

```bash
kubectl config current-context
kubectl get nodes -o wide
kubectl get storageclass
```

If provisioning or API TLS setup is needed, use the separate
[infrastructure guide](../enki.infra/docs/setup-dgx-spark.md).
This is a staging link; replace it with the final infrastructure repository URL
after extraction. Root privileges are only needed for host provisioning, not
for stack deployment.

## 2) Install the monitoring stack

```bash
./scripts/install-monitoring.sh --platform dgx-spark
```

Three Helm releases in the `observability` namespace, all pinned in the script:

- `kube-prometheus-stack` — prometheus-operator, Prometheus (15d / 50Gi on
  `local-path`), node-exporter, kube-state-metrics
- `grafana` — Grafana with the dashboard/datasource sidecars
- `tempo` — single-binary Tempo, 72h retention on a 20Gi `local-path` PVC,
  OTLP only (gRPC 4317 / HTTP 4318), not exposed outside the cluster

It also generates the `grafana-admin` Secret on first run; keep the value from
`kubectl -n observability get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d`
in a password manager.

This runs early because subsequent steps declare `ServiceMonitor` and
`PodMonitor` objects, whose CRDs this step installs, and
because the AI Gateway starts exporting spans to Tempo as soon as it comes up.

EnvoyProxy CRDs are installed by the gateway step, not by monitoring.

## 3) Install the NVIDIA GPU Operator

```bash
./scripts/install-gpu-operator.sh
```

Uses `helm/gpu-operator/values-gpu-operator.yaml`, which sets `driver.enabled=false`
since DGX OS already ships the NVIDIA driver — the operator only manages the
container toolkit, device plugin, and DCGM exporter.

The values file also enables the DCGM `ServiceMonitor`, so the Helm install
fails if step 2 has not run.

Verify:

```bash
kubectl -n gpu-operator get pods
kubectl get nodes -o json | jq '.items[].status.allocatable."nvidia.com/gpu"'
```

## 4) Install Envoy Gateway + Envoy AI Gateway

```bash
./scripts/install-ai-gateway.sh
```

Envoy AI Gateway was renamed upstream to **Agent Router** (same CRDs/API
group/namespaces/chart names) — see https://github.com/envoyproxy/ai-gateway.
Re-check the current chart version at https://theagentrouter.ai/docs before
pinning `--chart-version` in automation.

`helm/gateway/ai-gateway-values.yaml` also turns on GenAI tracing: the extProc
exports OTLP spans to `tempo.observability.svc.cluster.local:4317` using the
OpenTelemetry GenAI semantic conventions (`gen_ai.*` attributes), so step 2 must
have run first. Prometheus metrics stay enabled on `:1064`.

> Full prompts and responses are recorded in the spans
> (`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`). Anything sent
> through the gateway is readable in Grafana for the 72h retention window — set
> it to `false` if that is not acceptable.

Conversations are grouped by the `session.id` span attribute, which the gateway
copies from the `agent-session-id` request header:

```bash
curl -H 'agent-session-id: demo-1' -H 'Content-Type: application/json' \
  http://<gateway>/v1/chat/completions -d '{"model":"...","messages":[...]}'
```

Browse them in Grafana via the **GenAI Conversations** dashboard, with
throughput and latency on **AI Gateway Overview**.

## 5) Apply the DGX Spark kustomize overlay

```bash
./scripts/install-kserve.sh
kustomize build --load-restrictor=LoadRestrictionsNone k8s/overlays/dgx-spark | kubectl apply -f -
```

This applies:

- Shared namespaces from `k8s/base`
- cert-manager + a self-signed `ClusterIssuer` (kept for local certificates)
- Gateway API CRDs + `GatewayClass`/`Gateway` (Envoy Gateway)
- KServe model resources in **Standard** mode (no Knative/Istio), fronted by Gateway API
- Monitoring content from `k8s/monitoring`: the Grafana datasource, the
  vendored dashboards, and the scrape targets for KServe predictors,
  Envoy/AI Gateway and cert-manager
- model-owned Envoy AI Gateway routing CRs (`AIGatewayRoute`/`AIServiceBackend`)

## 6) One-command flow

```bash
./scripts/deploy.sh --platform dgx-spark
```

Add `--skip-gpu-operator` if GPU management is already installed. The command
does not support dry-run or cluster-provisioning flags; individual monitoring
and KServe installers retain their own dry-run options.

## 7) Verify

```bash
kubectl get nodes -o wide
kubectl get gatewayclass envoy-ai-gateway-basic
kubectl -n envoy-ai-gateway-system get gateway envoy-ai-gateway-basic
kubectl -n cert-manager get pods
kubectl -n kserve get pods
kubectl -n observability get pods
```

Check that every scrape target is up, and that the dashboards landed:

```bash
kubectl -n observability port-forward svc/kube-prometheus-stack-prometheus 9090:9090
# then open http://localhost:9090/targets

kubectl -n observability port-forward svc/grafana 3000:80
# then open http://localhost:3000
```

## 8) Deploy the default model (HuggingFace pull-through)

Model storage for DGX Spark starts with HuggingFace Hub pull-through — KServe's
built-in `huggingfaceserver` runtime pulls `storageUri: hf://<org>/<repo>`
directly, no separate model registry required yet.

```bash
kubectl apply -k models/default
kubectl get inferenceservice -n inference
```

The `default` service runs `Qwen/Qwen2.5-0.5B-Instruct` using vLLM and is wired
to the `default` model route in the Envoy AI Gateway. Configure the Cloudflare
tunnel routes described in [ADDONS.md](../k8s/addons/ADDONS.md), then use
`https://ai.zer0.garden/v1` as the OpenAI-compatible endpoint and
`https://ui.zer0.garden` for AnythingLLM.

## Roadmap / explicitly deferred

- **Kubeflow Model Registry** (`k8s/addons/kubeflow-model-registry`): still a
  placeholder addon; HuggingFace pull-through is the interim model source.
- **Auth**: no auth in front of Grafana/KServe endpoints yet (LAN-only,
  self-signed TLS). Planned: Envoy AI Gateway's built-in auth/rate-limiting.

## Upgrade policy

Same as the AKS path (see [setup-aks-kserve.md](setup-aks-kserve.md)): bump one
pinned upstream chart/URL at a time, apply, validate CRDs/webhooks/controller
health before the next bump.
