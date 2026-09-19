#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
K6_DIR="$ROOT_DIR/benchmark/k6"
K6_IMAGE="stack-k6-sse:latest"
RESULTS_DIR="$ROOT_DIR/benchmark/results"

BASE_URL="https://ai.zer0.garden"

MODEL_NAME="default"
MODEL_CONFIG="default"
SCENARIO="ramping_vus"

usage() {
  cat <<'EOF'
Run the k6 LLM benchmark suite against the Envoy AI Gateway and write results
to a local JSON file.

Usage:
  ./scripts/run-benchmark.sh [options] -- [extra k6 env vars, e.g. RAMP_MAX_VUS=20]

Options:
  --base-url <url>        OpenAI-compatible gateway base URL (default: https://ai.zer0.garden)
  --model-name <name>     Model name sent in the request payload
  --model-config <label>  Free-text label for this run, e.g. "maxtok256-temp0.7"
                          (included in the JSON summary metadata)
  --scenario <name>       ramping_vus | constant_arrival_rate | both (default: ramping_vus)
  --results-dir <path>    Directory for JSON result files (default: benchmark/results)
  -h, --help              Show this help

All other benchmark parameters (RAMP_MAX_VUS, ARRIVAL_RATE, MAX_TOKENS, ...)
are set via env vars - see benchmark/k6/llm-benchmark.js. Pass them after `--`,
e.g.:

  ./scripts/run-benchmark.sh --model-config baseline \
    -- RAMP_MAX_VUS=20 RAMP_HOLD=5m MAX_TOKENS=256
EOF
}

EXTRA_ENV=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url)
      BASE_URL="$2"; shift 2 ;;
    --model-name)
      MODEL_NAME="$2"; shift 2 ;;
    --model-config)
      MODEL_CONFIG="$2"; shift 2 ;;
    --scenario)
      SCENARIO="$2"; shift 2 ;;
    --results-dir)
      RESULTS_DIR="$2"; shift 2 ;;
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

for bin in curl docker; do
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

if ! curl --silent --fail "$BASE_URL/v1/models" >/dev/null 2>&1; then
  echo "[ERROR] AI Gateway did not respond at $BASE_URL/v1/models" >&2
  exit 1
fi

RUN_ID="$(date +%Y%m%dT%H%M%S)-${MODEL_CONFIG}"
mkdir -p "$RESULTS_DIR"
RESULTS_DIR="$(cd "$RESULTS_DIR" && pwd)"
RESULTS_FILE="$RESULTS_DIR/${RUN_ID}.json"
echo "[INFO] Running scenario=${SCENARIO} model_config=${MODEL_CONFIG} run_id=${RUN_ID}"
echo "[INFO] Writing JSON summary to ${RESULTS_FILE}"

DOCKER_ENV_ARGS=()
if [[ ${#EXTRA_ENV[@]} -gt 0 ]]; then
  for kv in "${EXTRA_ENV[@]}"; do
    DOCKER_ENV_ARGS+=(-e "$kv")
  done
fi

docker run --rm -i --network host \
  -v "$K6_DIR:/scripts:ro" \
  -v "$RESULTS_DIR:/results" \
  -e BASE_URL="$BASE_URL" \
  -e MODEL_NAME="$MODEL_NAME" \
  -e MODEL_CONFIG="$MODEL_CONFIG" \
  -e SCENARIO="$SCENARIO" \
  -e RUN_ID="$RUN_ID" \
  -e RESULTS_FILE="/results/${RUN_ID}.json" \
  ${DOCKER_ENV_ARGS[@]+"${DOCKER_ENV_ARGS[@]}"} \
  "$K6_IMAGE" run /scripts/llm-benchmark.js

echo "[INFO] Done. Results written to ${RESULTS_FILE}"
