#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
K6_DIR="$ROOT_DIR/benchmark/k6"
K6_IMAGE="stack-k6-sse:local"

NAMESPACE="ml-platform"
SERVICE="sample-model-predictor"
MODEL_PORT="18080"

OBS_NAMESPACE="observability"
PROMETHEUS_SERVICE="kube-prometheus-stack-prometheus"
PROMETHEUS_PORT="9090"

MODEL_HEADER=""
MODEL_NAME="Qwen/Qwen2.5-0.5B-Instruct"
MODEL_CONFIG="default"
SCENARIO="ramping_vus"

usage() {
  cat <<'EOF'
Run the k6 LLM benchmark suite against a KServe predictor and push results to
Prometheus (kube-prometheus-stack's remote-write receiver) for viewing in
Grafana's "LLM Benchmark (k6)" dashboard.

Usage:
  ./scripts/run-benchmark.sh [options] -- [extra k6 env vars, e.g. RAMP_MAX_VUS=20]

Options:
  --namespace <name>      Predictor namespace (default: ml-platform)
  --service <name>        Predictor Service (default: sample-model-predictor)
  --model-header <value>  x-ai-eg-model header value routed by the AI Gateway
                          (leave empty to hit the predictor Service directly)
  --model-name <name>     Model name sent in the request payload
  --model-config <label>  Free-text label for this run, e.g. "maxtok256-temp0.7"
                          (becomes the model_config tag in Grafana)
  --scenario <name>       ramping_vus | constant_arrival_rate | both (default: ramping_vus)
  -h, --help              Show this help

All other benchmark parameters (RAMP_MAX_VUS, ARRIVAL_RATE, MAX_TOKENS, ...)
are set via env vars - see benchmark/k6/llm-benchmark.js. Pass them after `--`,
e.g.:

  ./scripts/run-benchmark.sh --model-header sample-model --model-config baseline \
    -- RAMP_MAX_VUS=20 RAMP_HOLD=5m MAX_TOKENS=256
EOF
}

EXTRA_ENV=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace)
      NAMESPACE="$2"; shift 2 ;;
    --service)
      SERVICE="$2"; shift 2 ;;
    --model-header)
      MODEL_HEADER="$2"; shift 2 ;;
    --model-name)
      MODEL_NAME="$2"; shift 2 ;;
    --model-config)
      MODEL_CONFIG="$2"; shift 2 ;;
    --scenario)
      SCENARIO="$2"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    --)
      shift
      EXTRA_ENV=("$@")
      break
      ;;
    *)
      echo "[ERROR] Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

for bin in kubectl curl docker; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "[ERROR] Required binary not found: $bin" >&2
    exit 1
  fi
done

if ! docker image inspect "$K6_IMAGE" >/dev/null 2>&1; then
  echo "[ERROR] Docker image $K6_IMAGE not found. Build it first:" >&2
  echo "  docker build -t $K6_IMAGE $K6_DIR" >&2
  exit 1
fi

cleanup() {
  for pid in "${MODEL_PF_PID:-}" "${PROM_PF_PID:-}"; do
    [[ -n "$pid" ]] && kill "$pid" >/dev/null 2>&1 || true
  done
  wait >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

MODEL_URL="http://127.0.0.1:${MODEL_PORT}"
echo "[INFO] Forwarding ${SERVICE}.${NAMESPACE}.svc:80 to ${MODEL_URL}"
kubectl -n "$NAMESPACE" port-forward "svc/$SERVICE" "${MODEL_PORT}:80" >/tmp/run-benchmark-model-pf.log 2>&1 &
MODEL_PF_PID=$!

PROMETHEUS_URL="http://127.0.0.1:${PROMETHEUS_PORT}"
echo "[INFO] Forwarding ${PROMETHEUS_SERVICE}.${OBS_NAMESPACE}.svc:9090 to ${PROMETHEUS_URL}"
kubectl -n "$OBS_NAMESPACE" port-forward "svc/$PROMETHEUS_SERVICE" "${PROMETHEUS_PORT}:9090" >/tmp/run-benchmark-prom-pf.log 2>&1 &
PROM_PF_PID=$!

for _ in {1..60}; do
  if curl --silent --fail "$MODEL_URL/v1/models" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$MODEL_PF_PID" >/dev/null 2>&1; then
    cat /tmp/run-benchmark-model-pf.log >&2
    echo "[ERROR] Port-forward to the predictor exited before it became reachable" >&2
    exit 1
  fi
  sleep 2
done
if ! curl --silent --fail "$MODEL_URL/v1/models" >/dev/null 2>&1; then
  echo "[ERROR] Predictor did not become reachable at $MODEL_URL/v1/models" >&2
  exit 1
fi

for _ in {1..30}; do
  if curl --silent --fail "$PROMETHEUS_URL/-/ready" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$PROM_PF_PID" >/dev/null 2>&1; then
    cat /tmp/run-benchmark-prom-pf.log >&2
    echo "[ERROR] Port-forward to Prometheus exited before it became reachable" >&2
    exit 1
  fi
  sleep 2
done
if ! curl --silent --fail "$PROMETHEUS_URL/-/ready" >/dev/null 2>&1; then
  echo "[ERROR] Prometheus did not become reachable at $PROMETHEUS_URL" >&2
  exit 1
fi

RUN_ID="$(date +%Y%m%dT%H%M%S)-${MODEL_CONFIG}"
echo "[INFO] Running scenario=${SCENARIO} model_config=${MODEL_CONFIG} run_id=${RUN_ID}"

DOCKER_ENV_ARGS=()
for kv in "${EXTRA_ENV[@]}"; do
  DOCKER_ENV_ARGS+=(-e "$kv")
done

docker run --rm -i --network host \
  -v "$K6_DIR:/scripts:ro" \
  -e BASE_URL="$MODEL_URL" \
  -e MODEL_HEADER="$MODEL_HEADER" \
  -e MODEL_NAME="$MODEL_NAME" \
  -e MODEL_CONFIG="$MODEL_CONFIG" \
  -e SCENARIO="$SCENARIO" \
  -e RUN_ID="$RUN_ID" \
  -e K6_PROMETHEUS_RW_SERVER_URL="${PROMETHEUS_URL}/api/v1/write" \
  -e K6_PROMETHEUS_RW_TREND_STATS="p(50),p(95),p(99),min,max,avg" \
  "${DOCKER_ENV_ARGS[@]}" \
  "$K6_IMAGE" run --out experimental-prometheus-rw /scripts/llm-benchmark.js

echo "[INFO] Done. View results in Grafana under the 'LLM Benchmark (k6)' dashboard, run_id=${RUN_ID}"
