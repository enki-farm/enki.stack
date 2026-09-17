# Models

This directory contains KServe model packages, each deployable independently with `kubectl apply -k`.

## Model packages

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

- `infra/gateway/install-ai-gateway.sh`
