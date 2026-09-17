# Add-ons

This directory contains optional add-ons that can be applied independently with `kubectl apply -k`.

## 1) Cloudflared tunnel

Purpose: expose local or private services through a Cloudflare tunnel.

### Required setup

Edit the environment file before deploying:

```bash
cd k8s/addons/cloudflared
cat > .env <<'EOF'
token=YOUR_CLOUDFLARE_TUNNEL_TOKEN
EOF
```

The Kustomize config reads the `token` value from `.env` and creates the `tunnel-token` secret.

### Install

```bash
kubectl apply -k k8s/addons/cloudflared
```

Or, from inside the addon directory:

```bash
cd k8s/addons/cloudflared
kubectl apply -k .
```

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

## 2) AnythingLLM

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

## 3) Kubeflow Model Registry

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

## Quick reference

```bash
kubectl apply -k k8s/addons/cloudflared
kubectl apply -k k8s/addons/anythingllm
kubectl apply -k models/default
kubectl apply -k k8s/addons/kubeflow-model-registry
```

## End-to-end apply order

After installing the Envoy Gateway and AI Gateway controllers:

```bash
kubectl apply -k models/default
kubectl apply -k k8s/addons/anythingllm
kubectl apply -k k8s/addons/cloudflared
```

Then configure the two Cloudflare published hostnames using the steps above.

Use the `kubectl apply -k` command from the repository root unless you are working directly inside the addon directory.
