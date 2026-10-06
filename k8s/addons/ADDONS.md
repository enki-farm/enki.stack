# Add-ons

This directory contains optional add-ons that can be applied independently with `kubectl apply -k`.

## Optional public access

Cloudflared is managed separately in
[enki.infra](../../enki.infra/docs/cloudflared.md), not installed by the stack.
Replace this staging link with the final infrastructure repository URL after
extraction. The following is optional integration guidance for an existing tunnel.

### Configure Cloudflare published routes

The existing tunnel can publish both hostnames. No second tunnel is needed.
First, find the Envoy Gateway data-plane Service created for the shared AI Gateway:

```bash
kubectl -n envoy-gateway-system get svc | \
  grep envoy-envoy-ai-gateway-system-envoy-ai-gateway-basic

kubectl -n envoy-gateway-system get svc \
  -l gateway.envoyproxy.io/owning-gateway-name=envoy-ai-gateway-basic \
  -o jsonpath='{.items[0].metadata.name}{"\n"}'
```

If that label selector returns no Service, inspect the Gateway and its Services
and choose the Service listening on port 80:

```bash
kubectl -n envoy-ai-gateway-system get gateway envoy-ai-gateway-basic -o yaml
kubectl -n envoy-gateway-system get svc -o wide
```

In the Cloudflare dashboard, a single wildcard public hostname covers both
applications — Cloudflare forwards the original `Host` header by default, so
the Kubernetes routes can still distinguish `ai.zer0.garden` from
`ui.zer0.garden` behind the same tunnel route:

1. Open **Zero Trust > Networks > Tunnels** and select the existing tunnel.
2. Open **Public Hostnames** and add `*.zer0.garden`.
3. Set the service type to **HTTP** and set the URL to the Envoy Gateway Service,
  for example `http://envoy-envoy-ai-gateway-system-envoy-ai-gateway-basic-<hash>.envoy-gateway-system.svc.cluster.local:80`.
4. Confirm the wildcard DNS record is proxied through Cloudflare. Cloudflare may
  create it automatically when the published route is saved.

No per-hostname "preserve Host header" setting is needed: Cloudflare forwards
the original Host header to the tunnel by default.

The public connection is HTTPS at Cloudflare, while the tunnel connects to the
cluster over HTTP. The tunnel pod must be able to resolve the
`envoy-gateway-system.svc` cluster domain and reach the Envoy Service on port 80.

Verify the tunnel and routes:

```bash
kubectl -n cloudflare get pods -l pod=cloudflared
kubectl -n cloudflare logs deploy/cloudflared-deployment --tail=50
dig +short ai.zer0.garden
dig +short ui.zer0.garden
curl -I https://ui.zer0.garden
curl -i https://ai.zer0.garden/v1/models
```

The AI route is scoped to `ai.zer0.garden`, and the AnythingLLM UI route is
scoped to `ui.zer0.garden`; both must retain their original Host header.

---

## 1) AnythingLLM

Purpose: run the AnythingLLM app in-cluster.

### Required setup

Update the secret values in [k8s/addons/anythingllm/deployment.yaml](anythingllm/deployment.yaml) before applying:

```yaml
stringData:
  JWT_SECRET: change-me-anythingllm-jwt-secret
  OPEN_AI_KEY: sk-local-vllm
```

Replace the placeholder values with real secrets for your environment.

### Install

```bash
kubectl apply -k k8s/addons/anythingllm
```

Or:

```bash
cd k8s/addons/anythingllm
kubectl apply -k .
```

AnythingLLM is configured to use `https://ai.zer0.garden/v1` with model name
`default`, so apply the model and gateway resources before starting or restarting
the AnythingLLM deployment. The current manifests do not commit a public API key;
add the gateway's client-auth policy and provision the matching `OPEN_AI_KEY`
secret before exposing the AI hostname to untrusted clients.

---

## 2) Kubeflow Model Registry

Purpose: placeholder model-registry addon scaffold for future cluster work.

### Install

```bash
kubectl apply -k k8s/addons/kubeflow-model-registry
```

Or:

```bash
cd k8s/addons/kubeflow-model-registry
kubectl apply -k .
```

---

## 3) ComfyUI

Purpose: run ComfyUI on one GPU using `ghcr.io/enki-farm/comfyui` (built from
[docker/comfyui](../../docker/comfyui/Dockerfile)). Models, inputs, outputs and
custom nodes persist on the `comfyui-data` PVC mounted at `/data`.

### Install

```bash
kubectl apply -k k8s/addons/comfyui
kubectl -n comfyui port-forward svc/comfyui 8188:80
```

---

## 4) LibreChat

Purpose: run LibreChat via the official Helm chart
(`oci://ghcr.io/librechat-ai/librechat-chart/librechat`), using the enki AI
Gateway (`https://ai.zer0.garden/v1`, model `default`) as its only LLM endpoint.
The default OpenAI endpoint is disabled (`ENDPOINTS=custom,agents`).

[k8s/addons/librechat/values.yaml](librechat/values.yaml) is a copy of the
chart's upstream `values.yaml` with the enki changes applied.

### Required setup

```bash
cp k8s/addons/librechat/.env.example k8s/addons/librechat/.env
```

Set `ENKI_GATEWAY_API_KEY`. Empty credentials (`CREDS_KEY`, `CREDS_IV`,
`JWT_SECRET`, `JWT_REFRESH_SECRET`, `MEILI_MASTER_KEY`) are reused from the
existing Secret or generated once.

### Install

```bash
./k8s/addons/librechat/install-librechat.sh
./k8s/addons/librechat/install-librechat.sh --dry-run   # render only
```

The UI is published at `chat.zer0.garden` via the shared Envoy Gateway; the
wildcard Cloudflare hostname already covers it.

---

## Quick reference

```bash
kubectl apply -k k8s/addons/anythingllm
kubectl apply -k models/default
kubectl apply -k k8s/addons/kubeflow-model-registry
```

## End-to-end apply order

After installing the Envoy Gateway and AI Gateway controllers:

```bash
kubectl apply -k models/default
kubectl apply -k k8s/addons/anythingllm
```

If using an externally managed tunnel, configure its published hostnames using
the optional integration steps above. Tunnel installation is not a stack step.

Use the `kubectl apply -k` command from the repository root unless you are working directly inside the addon directory.
