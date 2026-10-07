# safe-qwen

This package deploys `Qwen/Qwen2.5-0.5B-Instruct` with vLLM through KServe and
routes AgentRouter requests for `safe-qwen` through the NeMo Guardrails service.
The Guardrails input regex rail rejects the standalone term `Dilbert`, ignoring
case. The model predictor is called directly by Guardrails and is not exposed by
this route.

## Prerequisites

1. Deploy the DGX Spark overlay so the `inference` namespace and
   `nemo-guardrails` service exist:

   ```bash
   ./scripts/deploy.sh --platform dgx-spark
   ```

2. Build and push the multi-architecture Guardrails image if it is not already
   available to the cluster:

   ```bash
   docker buildx build --platform linux/amd64,linux/arm64 \
     -t ghcr.io/enki-farm/nemo-server:latest --push docker/nemo-guardrails
   ```

   Ensure DGX Spark can pull the image. Configure an image pull secret if the
   GHCR package is private.

## Deploy

Apply this package separately; it is intentionally not part of either platform
overlay:

```bash
kubectl apply -k models/safe-qwen
kubectl -n inference get inferenceservice safe-qwen
```

The predictor downloads its model from Hugging Face at startup. This package
does not configure a persistent model cache. It requests one NVIDIA GPU, 4 CPU,
and 16Gi memory.

## Call

Send OpenAI-compatible chat-completion requests to `https://ai.zer0.garden/v1`
with the header `x-ai-eg-model: safe-qwen`. Input containing `Dilbert` as a
whole word is rejected before vLLM is called.