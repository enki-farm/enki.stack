#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
K6_SCRIPT="$ROOT_DIR/benchmark/k6/laya-benchmark.js"
K6_IMAGE="grafana/k6:latest"
RESULTS_DIR="$ROOT_DIR/benchmark/results"

TARGET_URL="https://ai.zer0.garden/laya"
MODEL_CONFIG="default"
VUS="10"
DURATION="30s"

usage() {
  cat <<'EOF'
Run the native Laya k6 benchmark with the standard Grafana k6 image.

Usage:
  ./scripts/run-laya-benchmark.sh [options] -- [extra k6 env vars]

Options:
  --target-url <url>       Laya endpoint (default: https://ai.zer0.garden/laya)
  --model-config <label>   Label included in result metadata
  --vus <count>            Number of constant virtual users (default: 10)
  --duration <duration>    How long to fire constant VUs, e.g. 1m, 30s (default: 30s)
  --results-dir <path>     Directory for JSON result files
  -h, --help               Show this help

Example:
  ./scripts/run-laya-benchmark.sh --vus 20 --duration 2m
EOF
}

EXTRA_ENV=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target-url)
      TARGET_URL="$2"; shift 2 ;;
    --model-config)
      MODEL_CONFIG="$2"; shift 2 ;;
    --vus)
      VUS="$2"; shift 2 ;;
    --duration)
      DURATION="$2"; shift 2 ;;
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

if ! command -v docker >/dev/null 2>&1; then
  echo "[ERROR] Required binary not found: docker" >&2
  exit 1
fi

RUN_ID="$(date +%Y%m%dT%H%M%S)-${MODEL_CONFIG}"
mkdir -p "$RESULTS_DIR"
RESULTS_DIR="$(cd "$RESULTS_DIR" && pwd)"
RESULTS_FILE="$RESULTS_DIR/${RUN_ID}.json"
echo "[INFO] Running Laya vus=${VUS} duration=${DURATION} model_config=${MODEL_CONFIG} run_id=${RUN_ID}"
echo "[INFO] Writing JSON summary to ${RESULTS_FILE}"

DOCKER_ENV_ARGS=()
if ((${#EXTRA_ENV[@]} > 0)); then
  for kv in "${EXTRA_ENV[@]}"; do
    DOCKER_ENV_ARGS+=(-e "$kv")
  done
fi

DOCKER_ARGS=(
  --rm -i --network host
  -v "$ROOT_DIR/benchmark/k6:/scripts:ro"
  -v "$RESULTS_DIR:/results"
  -e TARGET_URL="$TARGET_URL"
  -e MODEL_CONFIG="$MODEL_CONFIG"
  -e VUS="$VUS"
  -e DURATION="$DURATION"
  -e RUN_ID="$RUN_ID"
  -e RESULTS_FILE="/results/${RUN_ID}.json"
)
if ((${#DOCKER_ENV_ARGS[@]} > 0)); then
  DOCKER_ARGS+=("${DOCKER_ENV_ARGS[@]}")
fi
DOCKER_ARGS+=("$K6_IMAGE" run /scripts/laya-benchmark.js)

docker run "${DOCKER_ARGS[@]}"

echo "[INFO] Done. Results written to ${RESULTS_FILE}"