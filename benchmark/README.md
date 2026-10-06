# LLM benchmark suite (k6)

Load-tests the OpenAI-compatible `/v1/chat/completions` endpoint served through
the Envoy AI Gateway at `https://ai.zer0.garden`, measuring the metrics that
matter for real user conditions:

- **Time to first token (TTFT)** - `llm_ttft_milliseconds`
- **Inter-token latency / time per output token** - `llm_time_per_output_token_milliseconds`
- **End-to-end request latency** - `llm_e2e_latency_milliseconds`
- **Token throughput**, total and per active VU - `llm_completion_tokens` / `llm_prompt_tokens`
- **Request error rate** - `llm_request_errors`

Results are written to a local JSON summary file under `benchmark/results/`.

## Why a custom k6 image

Core k6's `http` module buffers the full response body before returning, so
it cannot time individual SSE chunks - which is what TTFT/inter-token latency
require. [`xk6-sse`](https://github.com/phymbert/xk6-sse) exposes each
Server-Sent Event with its own timestamp, so this suite ships a
`benchmark/k6/Dockerfile` that builds k6 with that extension baked in.

## One-time setup

Build the custom k6 image:

```bash
docker build -t stack-k6-sse:local benchmark/k6
```

## Running a benchmark

Deploy the model you want to test (see `k8s/overlays/*/examples/`), then:

```bash
./scripts/run-benchmark.sh \
  --model-config maxtok128-temp0.2 \
  --scenario ramping_vus \
  -- RAMP_MAX_VUS=10 RAMP_HOLD=2m MAX_TOKENS=128
```

The script runs the k6 container against `benchmark/k6/llm-benchmark.js` and
writes a k6 JSON summary to `benchmark/results/<run_id>.json`. The generated
`run_id`, `model_id`, and `model_config` are included in the summary metadata.

Use `--base-url` to point the benchmark at a different OpenAI-compatible
gateway.

To compare two configurations (e.g. different `max-model-len` or a different
InferenceService), deploy each one and run the script again with a different
`--model-config` label - each run gets its own JSON file.

### Scenarios

- `ramping_vus` (default): ramps virtual users up/hold/down, each simulating a
  user with think-time between turns and a mix of short/medium/long prompts
  (see `benchmark/k6/prompts.json`) - representative of real usage.
- `constant_arrival_rate`: fixed requests/sec regardless of response time,
  useful for finding the saturation point of a deployment.
- `both`: runs both scenarios in the same invocation.

See `benchmark/k6/llm-benchmark.js` for all tunable env vars
(`RAMP_MAX_VUS`, `ARRIVAL_RATE`, `MAX_TOKENS`, `TEMPERATURE`, `THINK_TIME_MS`, ...).

## Laya benchmark

The native Laya endpoint has a separate benchmark because it accepts typed
decision JSON rather than OpenAI chat completions. It uses the standard
`grafana/k6:latest` image and fires a constant number of virtual users for a
fixed duration, measuring request latency, response errors, and successful
predictions:

```bash
./scripts/run-laya-benchmark.sh --vus 20 --duration 2m
```

The default target is `https://ai.zer0.garden/laya`, with 10 VUs for 1 minute.
Override the target with `--target-url`; see `benchmark/k6/laya-benchmark.js`
for the request payload and timeout environment variables.

## Viewing results

Open the generated JSON file under `benchmark/results/`. It contains the k6
summary, including metric aggregates, thresholds, run metadata, and root group
checks.

## Single-file HTML report

Generate a self-contained HTML report for any one JSON result. The output path
defaults to the same file with an `.html` suffix, or can be selected with
`--output`:

```bash
python benchmark/report.py \
  benchmark/results/20260918T162303-default.json \
  --output benchmark/results/20260918T162303-default.html
```

The generated file embeds its CSS and data, so it can be opened directly
without a web server or network access.

## Caution

Generated JSON summaries are ignored by git because benchmark output is local
run data.
