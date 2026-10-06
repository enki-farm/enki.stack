# Laya KServe Predictor

This package runs the [Laya](https://github.com/NandhaKishorM/laya) decision
engine as a GPU-backed KServe custom predictor. Laya evaluates typed questions
over a state object in one forward pass and returns structured decisions rather
than generated text.

The upstream project is developed by Convai Innovations and is licensed under
the Apache License 2.0. This directory contains the HTTP/KServe packaging for
the upstream Python package; it does not contain or modify the model weights.

## API

The native predictor endpoint is exposed through KServe's custom predictor
protocol. This package keeps Laya's native request body unchanged:

```text
POST https://ai.zer0.garden/laya
Content-Type: application/json
```

Example request:

```json
{
  "state": {
    "subject": "Duplicate charge",
    "body": "I was billed twice for March."
  },
  "questions": {
    "department": {
      "type": "choice",
      "instructions": "Which department should handle this request?",
      "criteria": {
        "billing": "invoices, payments, refunds",
        "technical": "bugs, outages, system errors",
        "other": "everything else"
      }
    },
    "refund_requested": {
      "type": "noul",
      "instructions": "Does the user explicitly request a refund?"
    }
  }
}
```

The response is the native result returned by `Router.predict`, including
`answers` and routing metadata. An optional `model` field can select a specific
checkpoint, for example `english`, `multilingual`, or `typed-decisions`.

Internally, the route rewrites this request to KServe's
`/v1/models/laya:predict` endpoint. KServe health is available at
`/v1/models/laya`.

This is not an OpenAI-compatible completion endpoint. It is intentionally kept
separate from the repository's existing AI Gateway `/v1` route. The server is a
KServe `Model` subclass started by `ModelServer`, not a standalone FastAPI app.

## Runtime behavior

The container creates `laya.Router(preload=True)` once during startup. This
preloads the upstream checkpoints so language routing does not reload a model
on every request. Checkpoint files are downloaded from Hugging Face when the
pod starts and are not baked into the image.

The KServe manifest mounts an `emptyDir` cache at
`/home/laya/.cache/huggingface`. This avoids redownloading during a process
restart within the same pod, but a new pod downloads the checkpoints again.
Set `HF_HOME` to a persistent volume in an environment that needs cache
survival across pod replacement.

## Build and deploy

The published image target is:

```text
ghcr.io/enki-farm/predictor-laya:latest
```

Build and push the ARM64 image from an ARM64 builder or a configured Buildx
builder:

```bash
docker buildx build \
  --platform linux/arm64 \
  -t ghcr.io/enki-farm/predictor-laya:latest \
  --push \
  models/laya
```

Build a CPU image for local testing:

```bash
docker build -f models/laya/Dockerfile.cpu \
  -t predictor-laya:cpu \
  models/laya
docker run --rm -p 8080:8080 predictor-laya:cpu
```

Deploy the model package independently:

```bash
kubectl apply -k models/laya
kubectl get inferenceservice -n inference laya
```

The package creates the `laya` KServe service and an HTTPRoute for
`ai.zer0.garden/laya`. The shared Gateway must already be installed. Public
authentication is not configured by this repository, so protect the route at
the gateway or tunnel layer before exposing it beyond a trusted network.

## Configuration

The image defaults to `HF_HOME=/home/laya/.cache/huggingface`. The KServe
manifest requests one NVIDIA GPU, 4 CPUs, and 24 GiB of memory because the
preloaded Router keeps multiple checkpoints resident. Adjust these values in
`inferenceservice.yaml` if the target node has different capacity.

The current image and manifest target Linux ARM64 NVIDIA nodes such as the
DGX Spark path in this repository. The base image is an NVIDIA PyTorch image;
verify that the selected tag is available for the target architecture before
building.

## Local tests

The tests use a fake Router and do not download model weights. From this
directory, with the Python dependencies installed:

```bash
python -m pytest test_predictor.py
```

Test a running inference server with the standard-library client. The default
target is `https://ai.zer0.garden/laya`; override it with `--url`:

```bash
python models/laya/test_inference.py
python models/laya/test_inference.py --url http://localhost:8080/v1/models/laya:predict
python models/laya/test_inference.py --url https://example.test/laya --payload request.json
```