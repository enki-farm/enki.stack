# Roadmap

This document tracks planned work for enki.stack. Priorities reflect the order
currently listed in the project; items without a priority remain unranked.

## Priority 1

- Add DeepEval evaluation jobs and store raw JSON reports.
- Complete Hub browser authentication acceptance and harden production artifact
  storage.
- Improve platform hardening and documentation.

## Priority 2

- Add a vLLM semantic router.
- Add LibreChat scheduled agents.
- Add a self-hosted code interpreter for LibreChat.

## Unprioritized

- Hub: add database and OCI backups, Zot retention, and large-model ingress.
  See [later improvements](docs/kubeflow-hub.md#later-improvements). Existing
  Hugging Face caches remain unchanged.
- Configure Envoy AI Gateway client authentication and rate limiting. Public
  hostname routing is configured; authentication still requires a
  cluster-specific credential policy.
- Add tolerations to the NVIDIA GPU plugin so it does not run on CPU-only nodes
  in the AKS deployment path. DGX Spark uses the GPU Operator instead.
