# GPU Operator Time-Slicing

This directory configures GPU sharing **after** installing the NVIDIA GPU
Operator with Helm. It does not install the operator or take ownership of its
`ClusterPolicy`.

## What It Does

[gpu-time-slicing-configmap.yaml](./gpu-time-slicing-configmap.yaml) creates
`time-slicing-config` in the `gpu-operator` namespace. Its `any` configuration:

- Uses `migStrategy: none` and time-slices full GPUs rather than MIG instances.
- Advertises four shared GPU resources per physical GPU (`replicas: 4`).
- Names those resources `nvidia.com/gpu.shared` (`renameByDefault: true`).
- Rejects container requests for more than one shared GPU
  (`failRequestsGreaterThanOne: true`).

Time-slicing allows workloads to share a GPU's execution time. It does **not**
provide GPU memory or fault isolation, reserve one-quarter of GPU memory per
workload, or guarantee an equal share of compute. Workloads can still exhaust
the shared GPU's memory.

## Apply After Helm Installation

Run these commands from the repository root, against the intended cluster.
The operator must already be installed and its `ClusterPolicy` must exist.
These examples assume the operator namespace is `gpu-operator` and the policy
name is `cluster-policy`. If yours differ, update the ConfigMap's namespace and
the commands accordingly. `ClusterPolicy` itself is cluster-scoped.

```bash
kubectl apply -f k8s/gpu-operator/gpu-time-slicing-configmap.yaml

kubectl patch clusterpolicies.nvidia.com cluster-policy \
  --type=merge \
  -p '{"spec":{"devicePlugin":{"config":{"name":"time-slicing-config","default":"any"}}}}'
```

The merge patch changes only the referenced device-plugin configuration fields.
The operator reconciles the policy and rolls out the device-plugin configuration.
`default: any` selects the ConfigMap's `data.any` entry as the cluster-wide
default; explicit per-node `nvidia.com/device-plugin.config` labels can override
that selection.

A Kustomize patch cannot update this separately installed live policy unless the
policy is also a resource in the Kustomize build. For this Helm-managed setup,
use the live patch above instead of a patch in a platform overlay.

## Verify And Use

```bash
kubectl get clusterpolicies.nvidia.com cluster-policy \
  -o jsonpath='{.spec.devicePlugin.config}{"\n"}'

kubectl -n gpu-operator rollout status \
  daemonset/nvidia-device-plugin-daemonset --timeout=5m

kubectl get nodes \
  -o custom-columns='NAME:.metadata.name,GPU-SHARED:.status.allocatable.nvidia\.com/gpu\.shared'
```

The policy should reference `time-slicing-config` with default `any`. After
reconciliation, each participating node should advertise four shared resources
per physical GPU. A node without participating GPUs will have no shared count.
The rollout command alone does not confirm the requested configuration has
reconciled; also check the policy and node resources.

GPU workloads must request the renamed resource, for example in a container:

```yaml
resources:
  limits:
    nvidia.com/gpu.shared: 1
```

## Updates And Helm Upgrades

The operator does not automatically monitor time-slicing ConfigMap edits.
After changing the configuration, apply it and restart the device plugin to
reload it, then repeat the verification above:

```bash
kubectl apply -f k8s/gpu-operator/gpu-time-slicing-configmap.yaml
kubectl -n gpu-operator rollout restart daemonset/nvidia-device-plugin-daemonset
kubectl -n gpu-operator rollout status \
  daemonset/nvidia-device-plugin-daemonset --timeout=5m
```

Treat changes to sharing settings as a maintenance operation: they change the
GPU resources advertised to the scheduler and can affect GPU workloads.

Helm upgrades or another controller managing the policy may overwrite the live
patch. Verify the policy after upgrades and reapply the patch when necessary.
Keep this apply-and-patch step in the post-install/post-upgrade procedure.

See NVIDIA's [GPU time-slicing documentation](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/gpu-sharing.html)
for configuration details and troubleshooting.
