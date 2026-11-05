# LibreChat Agents

This package exposes a LibreChat agent through the shared
`envoy-ai-gateway-basic` Gateway. It keeps the LibreChat Agent API key in a
Kubernetes Secret generated locally from `.env`.

The Backend, AIServiceBackend, and AIGatewayRoute live in `inference`.
The Backend connects to `librechat-librechat.librechat.svc.cluster.local:3080`, so the
LibreChat Service stays in `librechat` without a cross-namespace Backend
reference or ReferenceGrant.

## Configure and install

From the repository root:

```bash
cp models/agents-librechat/.env.example models/agents-librechat/.env
chmod 600 models/agents-librechat/.env
# Set apiKey to a LibreChat Agent API key, not a Gateway client key.
kubectl apply -k models/agents-librechat
```

The `.env` file must contain the `apiKey` entry used by the Envoy AI Gateway
API-key policy. It is gitignored; do not commit it. Kustomize generates the
`agents-librechat-apikey` Secret in the `inference` namespace.

Send requests to the configured Gateway with
`x-ai-eg-model: nagra_qa` and the usual OpenAI-compatible request body.
The request's `model` should be `nagra_qa`. Set `modelNameOverride` in
`ai-gateway-route.yaml` to the desired LibreChat agent ID. The upstream path
is `/api/agents/v1/chat/completions`.

To diagnose routing failures, inspect the generated HTTPRoute, not only the
AIGatewayRoute: model listing and AIGatewayRoute acceptance do not establish
that the upstream Backend reference resolves.

```bash
kubectl -n inference get httproute agents-librechat -o yaml
```

Its `Accepted` and `ResolvedRefs` conditions should both be `True`.

To remove the route and its credentials, run:

```bash
kubectl delete -k models/agents-librechat
```
