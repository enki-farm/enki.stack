#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="ml-platform"
SERVICE="sample-model-predictor"
LOCAL_PORT="18080"
MODEL="Qwen/Qwen2.5-0.5B-Instruct"
PROMPT="Explain why GPU memory matters for language model inference in one sentence."

usage() {
  cat <<'EOF'
Test an OpenAI-compatible KServe predictor through a local port-forward.

Usage:
  ./scripts/test-inference.sh [options]

Options:
  --namespace <name>  Kubernetes namespace (default: ml-platform)
  --service <name>    Predictor Service (default: sample-model-predictor)
  --port <port>       Local port (default: 18080)
  --model <name>      Model name sent to vLLM
  --prompt <text>     User prompt
  -h, --help          Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace)
      NAMESPACE="$2"
      shift 2
      ;;
    --service)
      SERVICE="$2"
      shift 2
      ;;
    --port)
      LOCAL_PORT="$2"
      shift 2
      ;;
    --model)
      MODEL="$2"
      shift 2
      ;;
    --prompt)
      PROMPT="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[ERROR] Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if ! command -v kubectl >/dev/null 2>&1; then
  echo "[ERROR] kubectl is required" >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "[ERROR] curl is required" >&2
  exit 1
fi

cleanup() {
  if [[ -n "${PORT_FORWARD_PID:-}" ]]; then
    kill "$PORT_FORWARD_PID" >/dev/null 2>&1 || true
    wait "$PORT_FORWARD_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

API_URL="http://127.0.0.1:${LOCAL_PORT}"
echo "[INFO] Forwarding ${SERVICE}.${NAMESPACE}.svc:80 to ${API_URL}"
kubectl -n "$NAMESPACE" port-forward "svc/$SERVICE" "${LOCAL_PORT}:80" >/tmp/test-inference-port-forward.log 2>&1 &
PORT_FORWARD_PID=$!

for _ in {1..60}; do
  if curl --silent --fail "$API_URL/v1/models" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$PORT_FORWARD_PID" >/dev/null 2>&1; then
    cat /tmp/test-inference-port-forward.log >&2
    echo "[ERROR] Port-forward exited before the predictor became reachable" >&2
    exit 1
  fi
  sleep 2
done

if ! curl --silent --fail "$API_URL/v1/models" >/dev/null 2>&1; then
  echo "[ERROR] Predictor did not become reachable at $API_URL/v1/models" >&2
  exit 1
fi

echo "[INFO] Sending chat completion request"
curl --fail-with-body --silent --show-error \
  -X POST "$API_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "$(printf '{"model":"%s","messages":[{"role":"user","content":"%s"}],"max_tokens":64,"temperature":0.2}' "$MODEL" "$PROMPT")"
printf '\n'
