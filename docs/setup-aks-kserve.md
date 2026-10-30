# enki.stack AKS + KServe Setup

Install on an existing AKS cluster using the [profile-driven CLI](cli.md).
For the primary single-box target, see [DGX Spark setup](setup-dgx-spark.md).
Provisioning and cloud networking remain separate from stack installation.

## Prerequisites

- Python 3.12+, Helm 3/4 and kubectl compatible with the cluster.
- Ready nodes and permissions to install cluster-scoped CRDs/controllers.
- `managed-csi` storage for persistent components, LoadBalancer support and
  access to charts/images.
- Existing GPU driver/device-plugin support for GPU inference. CPU-only partial
  controller installations do not require advertised GPUs.
- DNS/network access to the shared HTTPS gateway and an identity provider for
  authenticated components.

See the separate [AKS infrastructure guide](../enki.infra/docs/setup-aks.md).
Replace this staging link with the infrastructure repository URL after extraction.
The CLI does not invoke Azure CLI, provision nodes or retrieve kubeconfig.

## Configure and install

```bash
python -m pip install -e .
enkictl init -p profiles/aks/profile.yaml
enkictl validate -p profiles/aks/profile.yaml --render
enkictl install -p profiles/aks/profile.yaml --dry-run
enkictl install -p profiles/aks/profile.yaml
```

Select platform `aks` and GPU mode `existing`. Certificate options are Azure
DNS-01, Cloudflare DNS-01 and self-signed; cloud DNS zones/IAM are external.
The CLI always uses the explicit context in the profile.

The full core includes certificates, Envoy/AI Gateway, Dex, monitoring, KServe,
and [standalone Hub](kubeflow-hub.md), not GPU Operator, models or optional apps.
MySQL, PostgreSQL and Zot use `managed-csi` PVCs and are single-instance, not HA.
Verify NetworkPolicy enforcement with the target AKS CNI.

## Component operations

```bash
enkictl component install auth kserve -p profiles/aks/profile.yaml --dry-run
enkictl component install auth kserve -p profiles/aks/profile.yaml
enkictl component install monitoring kubeflow-hub -p profiles/aks/profile.yaml
enkictl component list -p profiles/aks/profile.yaml
enkictl component uninstall kubeflow-hub -p profiles/aks/profile.yaml --dry-run
```

Dependencies are included automatically. GPU Operator is deliberately unsupported
on AKS; manage nodes/device plugins through the infrastructure owner. Named
removal preserves dependencies and data by default. See the CLI guide for
selected purge, confirmations and recovery inventory.

AI Gateway without monitoring warns about missing Tempo. Monitoring owns scrape
resources; scrape selectors for absent workloads do not require installing them.

Do not apply the legacy whole-stack overlays alongside the CLI: they bypass its
ownership tracking and can install separate models. Existing shell resources and
old CLI inventory are not adopted.

## Verify and upgrade

```bash
kubectl --context YOUR_AKS_CONTEXT -n cert-manager get pods
kubectl --context YOUR_AKS_CONTEXT -n kserve get pods
kubectl --context YOUR_AKS_CONTEXT -n observability get pods
kubectl --context YOUR_AKS_CONTEXT -n kubeflow get pods
```

Verify certificate issuance, browser login/group denial, storage, network policy
and inference on a disposable AKS cluster. Local rendering is not production
certification. Pins live in [helm/platform.yaml](../helm/platform.yaml); change one
upstream dependency at a time and validate its CRDs, webhooks and controllers.
