# External OpenAI

This package routes requests to OpenAI's `gpt-4o-mini` model. It uses the
shared `envoy-ai-gateway-basic` Gateway and keeps the upstream API key in a
Kubernetes Secret generated locally from `.env`.

## Configure and install

From the repository root:

```bash
cp models/external-openai/.env.example models/external-openai/.env
chmod 600 models/external-openai/.env
# Replace the example value in models/external-openai/.env with your OpenAI API key.
kubectl apply -k models/external-openai
```

The `.env` file must contain the `apiKey` entry used by the Envoy AI Gateway
API-key policy. It is gitignored; do not commit it. Kustomize generates the
`external-openai-apikey` Secret in the `inference` namespace.

Inbound requests also require a separate Gateway client key in
`Authorization: Bearer <gateway-api-key>`. The route carries
`require-api-key: "true"`; install the shared
[machine-access policy](../../docs/authentication.md#machine-access-gateway-api-keys)
with `kubectl apply -k models` before exposing it. The upstream OpenAI key is
not the Gateway client credential.

Send requests to the configured Gateway with
`x-ai-eg-model: gpt-4o-mini` and the usual OpenAI-compatible request body.
The request's `model` should be `gpt-4o-mini`.

To remove the route and its credentials, run:

```bash
kubectl delete -k models/external-openai
```
