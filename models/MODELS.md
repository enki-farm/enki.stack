# Models

This directory contains KServe model packages, each deployable independently with `kubectl apply -k`.

## Model packages

The [bge-embeddings](bge-embeddings/README.md) package is an internal CPU-only
embedding service for LibreChat RAG on DGX Spark. Install it independently
before the LibreChat addon; it does not create public gateway routes.

The optional [default-rate-limited](default-rate-limited/README.md) package is
an authenticated alias of the existing default backend, not another KServe
workload. Install it with its helper after enabling the Redis-backed limiter;
it is intentionally excluded from the default package and platform overlays.

Purpose: deploy a KServe model and its model-specific AI Gateway resources. The
shared Gateway, ClientTrafficPolicy, and EnvoyProxy live under
`k8s/gateway-api`; each model owns its Backend, AIServiceBackend, and
AIGatewayRoute under `models/<model>`.

### Install

```bash
kubectl apply -k models/default
```

Or:

```bash
cd models/default
kubectl apply -k .
```

For the controller installation prerequisites, see:

- `scripts/install-ai-gateway.sh`
