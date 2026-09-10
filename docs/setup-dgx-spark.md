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

### Troubleshooting: adding a TLS SAN after the initial install

If `kubectl` on another machine fails with something like:

```
Unable to connect to the server: tls: failed to verify certificate: x509: certificate is valid for
kubernetes, kubernetes.default, kubernetes.default.svc, kubernetes.default.svc.cluster.local,
localhost, spark-09c0, not spark-09c0.local
```

the kubeconfig's `server:` hostname isn't in the k3s serving cert's SAN list yet.
Re-run the install script on the DGX Spark box with `--tls-san` to add it —
k3s re-applies `INSTALL_K3S_EXEC` and regenerates the dynamic serving
certificate to include the new SAN:

```bash
sudo ./infra/k3s/install-k3s.sh --tls-san spark-09c0.local
```

Use `sudo`, even though the first install may have been run as your own user:
`/etc/rancher/k3s/k3s.yaml` is root-owned (mode `600`), and this script's
post-install steps (`kubectl ... wait --for=condition=Ready` and copying the
kubeconfig) read that file directly rather than shelling out through `sudo`
themselves. Running the whole script unprivileged fails at the "waiting for
node" step with `open /etc/rancher/k3s/k3s.yaml: permission denied` — the k3s
installer itself still succeeds (it escalates internally), but the wrapper
script can't read the freshly written kubeconfig afterwards.

## 2) Install the monitoring stack

```bash
./infra/monitoring/install-monitoring.sh --platform dgx-spark
```

Two Helm releases in the `observability` namespace, both pinned in the script:

- `kube-prometheus-stack` — prometheus-operator, Prometheus (15d / 50Gi on
  `local-path`), node-exporter, kube-state-metrics
- `grafana` — Grafana with the dashboard/datasource sidecars

It also generates the `grafana-admin` Secret on first run; keep the value from
`kubectl -n observability get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d`
in a password manager.

This runs early because everything after it declares `ServiceMonitor`,
`PodMonitor` or `EnvoyProxy` objects, whose CRDs this step installs.

## 3) Install the NVIDIA GPU Operator

```bash
./infra/k3s/install-gpu-operator.sh
```

Uses `infra/k3s/values-gpu-operator.yaml`, which sets `driver.enabled=false`
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
./infra/gateway/install-envoy-gateway.sh
./infra/gateway/install-ai-gateway.sh
```

Envoy AI Gateway was renamed upstream to **Agent Router** (same CRDs/API
group/namespaces/chart names) — see https://github.com/envoyproxy/ai-gateway.
Re-check the current chart version at https://theagentrouter.ai/docs before
pinning `--chart-version` in automation.

## 5) Apply the DGX Spark kustomize overlay

```bash
kustomize build --load-restrictor=LoadRestrictionsNone k8s/overlays/dgx-spark | kubectl apply -f -
```

This applies:

- Shared namespaces from `k8s/base`
- cert-manager + a self-signed `ClusterIssuer` (no public domain needed) and a
  `Certificate` for the KServe ingress Gateway's HTTPS listener
- Gateway API CRDs + `GatewayClass`/`Gateway` (Envoy Gateway)
- KServe in **RawDeployment** mode (no Knative/Istio), fronted by Gateway API
- Monitoring content from `k8s/monitoring`: the Grafana datasource, the
  vendored dashboards, and the scrape targets for KServe predictors,
  Envoy/AI Gateway and cert-manager
- Envoy AI Gateway routing CRs (`AIGatewayRoute`/`AIServiceBackend`) — placeholder
  wiring, adjust to your actual model backends

## 6) One-command flow

```bash
./scripts/deploy-dgx-spark.sh
```

## 7) Verify

```bash
kubectl get nodes -o wide
kubectl get gatewayclass envoy
kubectl -n kserve get gateway kserve-ingress-gateway
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

## 8) Deploy a sample model (HuggingFace pull-through)

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
