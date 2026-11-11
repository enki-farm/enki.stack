kubectl get secret --namespace observability grafana-admin -o jsonpath="{.data.admin-password}" | base64 --decode ; echo

The Grafana chart shadows the image's bundled-plugin directory with a writable
volume because the container root filesystem is read-only. Keep the bundled
plugin IDs required by this deployment explicitly listed in
`values-grafana.yaml`; unlisted bundled plugins are not available after shadowing.

## Resource accounting

Stack workloads declare CPU and memory requests and limits, including monitoring
sidecars and init containers. Requests are scheduler reservations, not measured
consumption; limits are ceilings, not reservations. The initial budgets are
conservative service budgets, not load-tested capacity requirements. Existing
budgets, including inference GPU allocations, are preserved.

Use the Kubernetes Views dashboards to compare usage with requests/limits.
`container_memory_working_set_bytes` is not the same as host used memory: host
services, the kernel, caches and (on DGX Spark) shared CPU/GPU memory also consume
RAM. CPU limits can throttle bursts and memory limits can trigger OOM kills;
review peaks, throttling and restarts under representative load before tightening
these budgets.

For regular containers, kube-state-metrics exposes
`kube_pod_container_resource_requests` and `kube_pod_container_resource_limits`
with `resource="cpu"` or `resource="memory"`. Init-container metrics are separate:
`kube_pod_init_container_resource_requests` and
`kube_pod_init_container_resource_limits`. Do not add all init containers to the
regular-container total: scheduling uses the maximum concurrent startup demand
versus steady state, accounting for restartable init sidecars and pod overhead.
Limits can be overcommitted; their sum is not the node capacity required to
schedule the pods.

After restarting Prometheus and kube-state-metrics together, old and new
exporter-instance series can overlap for the Prometheus lookback window
(normally five minutes). Let these stale series expire before comparing totals.
Unfiltered resource sums can also include completed pods; use pod phase filters
when calculating current reservations.

The AI Gateway processor is a restartable init sidecar configured through
`k8s/gateway-api/gateway-config.yaml`; the Envoy shutdown sidecar is configured
through the EnvoyProxy deployment patch. GPU Operator's component values cover
its main containers and are inherited by several generated init containers and
config-manager sidecars. There are no independent resource fields for those
containers; for generated pods/hooks still missing fields,
`k8s/gpu-operator/resource-defaults.yaml` supplies namespace-scoped defaults for
missing fields only. Apply this LimitRange before creating GPU pods; it does not
retroactively update existing pods.
The Helm post-renderer supplies small defaults for upstream connectivity-test
pods that expose no resource values, without replacing explicit test budgets.

Reconcile affected installed components and addons using the normal profile:

```bash
enkictl component install certificates ai-gateway monitoring gpu-operator \
  -p profiles/local/profile.yaml --yes
enkictl addon install activepieces -p profiles/local/profile.yaml --yes
enkictl addon install librechat -p profiles/local/profile.yaml --yes
```

These operations restart changed workloads. Do not install components/addons
that are not already wanted on the target cluster. K3s system services and
externally managed workloads are outside this repository's resource policy;
include them separately when planning whole-node capacity.