# LLM benchmark suite (k6)

Load-tests the OpenAI-compatible `/v1/chat/completions` endpoint served by a
KServe `InferenceService` (through the Envoy AI Gateway or directly against
the predictor Service), measuring the metrics that matter for real user
conditions:

- **Time to first token (TTFT)** - `llm_ttft_milliseconds`
- **Inter-token latency / time per output token** - `llm_time_per_output_token_milliseconds`
- **End-to-end request latency** - `llm_e2e_latency_milliseconds`
- **Token throughput**, total and per active VU - `llm_completion_tokens` / `llm_prompt_tokens`
- **Request error rate** - `llm_request_errors`

Results are pushed straight to Prometheus (kube-prometheus-stack's
remote-write receiver) and visualized in Grafana's **LLM Benchmark (k6)**
dashboard (`k8s/monitoring/dashboards/k6-benchmark.json`), alongside the
existing **LLM Inference** dashboard (server-side `vllm:*`/`gen_ai_*` metrics).

## Why a custom k6 image

Core k6's `http` module buffers the full response body before returning, so
it cannot time individual SSE chunks - which is what TTFT/inter-token latency
require. [`xk6-sse`](https://github.com/phymbert/xk6-sse) exposes each
Server-Sent Event with its own timestamp, so this suite ships a
`benchmark/k6/Dockerfile` that builds k6 with that extension baked in.

## One-time setup

1. Enable the Prometheus remote-write receiver (already set in
   `infra/monitoring/values-kube-prometheus-stack.yaml` -
   `prometheusSpec.enableRemoteWriteReceiver: true`). Re-run
   `./infra/monitoring/install-monitoring.sh --platform <aks|dgx-spark>` if
   you installed monitoring before this change.
2. Build the custom k6 image:

   ```bash
   docker build -t stack-k6-sse:local benchmark/k6
   ```

## Running a benchmark

Deploy the model you want to test (see `k8s/overlays/*/examples/`), then:

```bash
./scripts/run-benchmark.sh \
  --model-header sample-model \
  --model-name Qwen/Qwen2.5-0.5B-Instruct \
  --model-config maxtok128-temp0.2 \
  --scenario ramping_vus \
  -- RAMP_MAX_VUS=10 RAMP_HOLD=2m MAX_TOKENS=128
```

The script port-forwards to the predictor Service and to Prometheus, runs the
k6 container against `benchmark/k6/llm-benchmark.js`, and tags every metric
with `model_id`, `model_config` and a generated `run_id` so runs can be
compared side by side in Grafana.

`--model-header` is the `x-ai-eg-model` value the Envoy AI Gateway routes on
(see `models/default/ai-gateway-route.yaml`); leave it empty to
hit the predictor Service directly and skip the gateway.

To compare two configurations (e.g. different `max-model-len` or a different
InferenceService), deploy each one and run the script again with a different
`--model-config` label - both runs land in Prometheus and can be filtered
side-by-side via the dashboard's `$model_config` variable.

### Scenarios

- `ramping_vus` (default): ramps virtual users up/hold/down, each simulating a
  user with think-time between turns and a mix of short/medium/long prompts
  (see `benchmark/k6/prompts.json`) - representative of real usage.
- `constant_arrival_rate`: fixed requests/sec regardless of response time,
  useful for finding the saturation point of a deployment.
- `both`: runs both scenarios in the same invocation (harder to separate in
  Grafana's time range - prefer separate invocations per scenario).

See `benchmark/k6/llm-benchmark.js` for all tunable env vars
(`RAMP_MAX_VUS`, `ARRIVAL_RATE`, `MAX_TOKENS`, `TEMPERATURE`, `THINK_TIME_MS`, ...).

## Viewing results

```bash
kubectl -n observability port-forward svc/grafana 3000:80
```

Open Grafana, go to the **LLM Benchmark (k6)** dashboard, and use the
`model_id` / `model_config` / `run_id` variables to filter to the run(s) you
care about.

## Caution

`run_id` is a Prometheus label generated per invocation; this is convenient
for comparing runs in Grafana but increases label cardinality over time. Given
the short Prometheus retention in this stack (7-15 days, see
`infra/monitoring/values-kube-prometheus-stack-*.yaml`), this is fine for
ad-hoc benchmarking. Revisit if you start running this suite continuously.
