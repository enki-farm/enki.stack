#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${COMFYUI_DATA_DIR:-/data}"

mkdir -p "${DATA_DIR}"/{models,input,output,temp,user,custom_nodes}

# Keep image-provided extensions available when custom_nodes is volume-backed.
if [ ! -e "${DATA_DIR}/custom_nodes/ComfyUI-Manager" ]; then
  cp -a /comfyui/custom_nodes/ComfyUI-Manager "${DATA_DIR}/custom_nodes/"
fi

# Custom nodes live on the volume, so their Python deps must be (re)installed into the image venv.
for req in "${DATA_DIR}"/custom_nodes/*/requirements.txt; do
  [ -f "${req}" ] || continue
  echo "Installing custom node requirements: ${req}"
  uv pip install --no-cache -r "${req}" || echo "WARN: failed to install ${req}" >&2
done

echo "Starting ComfyUI $(cat /comfyui/VERSION)"
# Use COMFYUI_LISTEN_PORT (not COMFYUI_PORT) since Kubernetes auto-injects
# COMFYUI_PORT=tcp://<clusterIP>:80 for any Service named "comfyui" in the namespace.
exec python /comfyui/main.py \
  --listen 0.0.0.0 \
  --port "${COMFYUI_LISTEN_PORT:-8188}" \
  --base-directory "${DATA_DIR}" \
  "$@"
