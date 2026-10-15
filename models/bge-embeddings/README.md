# BGE CPU Embeddings

Independent, internal KServe model for LibreChat RAG on DGX Spark. It uses
`BAAI/bge-small-en-v1.5`, served as `embedding`, through the OpenAI-compatible
`/v1/embeddings` endpoint. No GPU or AI Gateway route is created.

## Install

KServe, the `inference` namespace, and its existing `huggingface-cache` PVC
must already exist. The cluster must have an ARM64 node. The shared claim is
defined in [models/pvc_hf_cache.yaml](../pvc_hf_cache.yaml); this package does
not create or resize it. Its ReadWriteOnce access permits sharing by pods on
the same node; local-path storage also ties it to the node holding its data.

```bash
kubectl apply -k models/bge-embeddings
kubectl wait -n inference --for=condition=Ready inferenceservice/bge-embeddings --timeout=15m
kubectl get service -n inference bge-embeddings-predictor
```

The RAG API connects directly to
`http://bge-embeddings-predictor.inference.svc.cluster.local/v1` on service port
80. The model listens on container port 8080. KServe Standard deployment mode
keeps one replica available; there is no scale-to-zero dependency.

The image is `vllm/vllm-openai-cpu:v0.31.0-arm64`, pinned by digest in the
manifest. Model and tokenizer revision are
`5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`. The model requests two CPUs and 2Gi
RAM, limited to four CPUs and 4Gi, with two inference threads. It reuses the
existing 100Gi `huggingface-cache` PVC for downloads. First startup may take
several minutes.
Eager execution avoids startup compilation for this small model. Generation
KV cache memory is explicitly zero; pooling does not require a generation
cache, and this avoids vLLM's default host-memory reservation check on CPU.

## Smoke Test

```bash
kubectl port-forward -n inference service/bge-embeddings-predictor 8081:80
```

In another terminal:

```bash
curl -fsS http://localhost:8081/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d '{"model":"embedding","input":["Paris is the capital of France.","Database transactions use locks."],"encoding_format":"float"}'
```

Expect two finite, normalized, 384-dimensional embeddings in input order.
The model uses CLS pooling from its Sentence Transformers configuration.
The upstream RAG API calls this service using its OpenAI embeddings provider.

For a reproducible ARM64 CPU test without a cluster:

```bash
docker run --rm --platform linux/arm64 --cpus 4 --memory 4g --shm-size 512m \
  -e VLLM_CPU_OMP_THREADS_BIND=nobind -e OMP_NUM_THREADS=2 \
  -v "$PWD/models/bge-embeddings/test_server.py:/test_server.py:ro" \
  --entrypoint python \
  vllm/vllm-openai-cpu:v0.31.0-arm64@sha256:045fb648a61cfcd32d4b9ca5d0c391013afadae0178d2e23ac292597f2f64036 \
  /test_server.py -v
```

## Limits

This is a small English model with a 512-token input budget, including special
tokens. Upstream RAG chunk sizes count characters, not model tokens; its
400-character passages and 40-character overlap are starting values rather
than guaranteed token-safe limits. Test dense text and long queries for input
overflow. No custom tokenization or query-prefix layer is deployed.
Corpus changes must retain the same
model, revision, pooling and dimensions. Changing any of those requires a new
collection and re-embedding original documents, even if dimensions match.

The PVC persists pod restarts, not disk or node loss. Keep original documents
and database backups outside the Spark node.