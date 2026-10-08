# Add-ons

This directory contains optional add-ons that can be applied independently with `kubectl apply -k`.

## Optional public access

Cloudflared is managed separately in
[enki.infra](../../enki.infra/docs/cloudflared.md), not installed by the stack.
Replace this staging link with the final infrastructure repository URL after
extraction. The following is optional integration guidance for an existing tunnel.

### Configure Cloudflare published routes

The existing tunnel can publish the AI API and LibreChat UI hostnames. No
second tunnel is needed.
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
the Kubernetes routes can distinguish `ai.zer0.garden` from
`chat.zer0.garden` behind the same tunnel route:

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
dig +short chat.zer0.garden
curl -I https://chat.zer0.garden
curl -i https://ai.zer0.garden/v1/models
```

The AI route is scoped to `ai.zer0.garden`, and the LibreChat UI route is
scoped to `chat.zer0.garden`; both must retain their original Host header.

---

## 1) Kubeflow Model Registry

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

## 2) ComfyUI

Purpose: run ComfyUI on one GPU using `ghcr.io/enki-farm/comfyui` (built from
[docker/comfyui](../../docker/comfyui/Dockerfile)). Models, inputs, outputs and
custom nodes persist on the `comfyui-data` PVC mounted at `/data`.

### Install

```bash
kubectl apply -k k8s/addons/comfyui
kubectl -n comfyui port-forward svc/comfyui 8188:80
```

---

## 3) LibreChat

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

Web search uses the internal [SearXNG](#7-searxng) instance for search and
[Crawl4AI](#6-crawl4ai) (through its Firecrawl shim) for scraping, without a
reranker. Install both first; `FIRECRAWL_API_KEY` is read from the
`crawl4ai-credentials` Secret when left empty in `.env`. Both in-cluster hosts are
listed under `webSearch.allowedAddresses`, otherwise LibreChat's SSRF guard
blocks them.

---

## 4) NeMo Guardrails

The DGX Spark overlay deploys the NeMo Guardrails server in namespace `inference`
using `ghcr.io/enki-farm/nemo-server:latest`. Build and publish the image for
both DGX Spark and amd64 nodes before deployment:

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  -t ghcr.io/enki-farm/nemo-server:latest --push docker/nemo-guardrails
```

Ensure the cluster can pull the image; configure an image pull secret if the
GHCR package is private. The service exposes an OpenAI-compatible API internally
on port 80 and forwards allowed prompts directly to the `safe-qwen` KServe
predictor. Its input regex rail rejects the case-insensitive whole word
`Dilbert`. Apply the corresponding model and AgentRouter route separately with
`kubectl apply -k models/safe-qwen` after the DGX Spark overlay is deployed.

---

## 5) Crawl4AI

Purpose: internal web crawler ([Crawl4AI](https://docs.crawl4ai.com/core/self-hosting/)
`unclecode/crawl4ai:0.9.2`) plus a Firecrawl-compatible `/v1|v2/scrape` shim
sidecar ([docker/firecrawl-shim](../../docker/firecrawl-shim/app.py)) so LibreChat
web search can use it as its Firecrawl scraper. Not exposed publicly:

| Endpoint | Purpose |
| --- | --- |
| `http://crawl4ai.crawl4ai.svc.cluster.local:11235` | Crawl4AI REST API (Bearer `CRAWL4AI_API_TOKEN`) |
| `http://crawl4ai.crawl4ai.svc.cluster.local:3002` | Firecrawl shim (Bearer `FIRECRAWL_SHIM_API_KEY`) |

[config/config.yml](crawl4ai/config/config.yml) is a pinned copy of the upstream
v0.9.2 server config (it replaces the image default); it sets the LLM provider
to `openai/default` (via `LLM_BASE_URL=https://ai.zer0.garden/v1`) and caps
concurrent pages at 8. Crawl4AI's own SSRF guard blocks private and cluster
addresses; the NetworkPolicy additionally limits egress to the public internet
and ingress to the `librechat` namespace (only if the CNI enforces policies).
LibreChat also connects to Crawl4AI's built-in SSE MCP endpoint at
`/mcp/sse`; the installer copies the existing `CRAWL4AI_API_TOKEN` into
LibreChat's Secret. In an Agent, add the `crawl4ai` MCP server and enable its
`md` tool to fetch a user-provided public URL directly. Keep other Crawl4AI MCP
tools disabled unless the Agent needs them.

### Build the shim image

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  -t ghcr.io/enki-farm/firecrawl-shim:latest --push docker/firecrawl-shim
```

### Install

```bash
cp k8s/addons/crawl4ai/.env.example k8s/addons/crawl4ai/.env   # optional
./k8s/addons/crawl4ai/install-crawl4ai.sh
./k8s/addons/crawl4ai/install-crawl4ai.sh --dry-run   # render only
```

After changing a secret value, restart the pod:
`kubectl -n crawl4ai rollout restart deploy/crawl4ai`.

### Test with Bruno

```bash
kubectl -n crawl4ai port-forward svc/crawl4ai 11235:11235 3002:3002
cp .bruno/.env.example .bruno/.env   # fill in from the crawl4ai-credentials Secret
```

Select the `local-port-forward` environment and run the `Crawl4AI - *` and
`Firecrawl Shim - *` requests. The Crawl4AI dashboard is at
`http://localhost:11235/dashboard` (paste the API token).

---

## 6) SearXNG

Purpose: internal meta search engine (`searxng/searxng`) used as LibreChat's web
search provider. JSON output is enabled and the limiter is off because the
instance is only reachable in-cluster at
`http://searxng.searxng.svc.cluster.local:8080`.

### Install

```bash
./k8s/addons/searxng/install-searxng.sh
kubectl -n searxng port-forward svc/searxng 8080:8080   # Bruno: SearXNG - Search JSON
```

---

## Quick reference

```bash
kubectl apply -k models/default
kubectl apply -k k8s/addons/kubeflow-model-registry
./k8s/addons/crawl4ai/install-crawl4ai.sh
./k8s/addons/searxng/install-searxng.sh
./k8s/addons/librechat/install-librechat.sh
```

## End-to-end apply order

After installing the Envoy Gateway and AI Gateway controllers:

```bash
kubectl apply -k models/default
```

If using an externally managed tunnel, configure its published hostnames using
the optional integration steps above. Tunnel installation is not a stack step.

Use the `kubectl apply -k` command from the repository root unless you are working directly inside the addon directory.
