# syntax=docker/dockerfile:1
# Image: ghcr.io/enki-farm/comfyui
# Build (multi-arch, amd64 + arm64/DGX Spark):
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -t ghcr.io/enki-farm/comfyui:latest --push docker/comfyui

ARG BASE_IMAGE=nvidia/cuda:12.8.1-base-ubuntu24.04

FROM ghcr.io/astral-sh/uv:latest AS uv

FROM ${BASE_IMAGE}

# "latest" resolves to the newest vX.Y.Z release tag at build time; pass e.g. v0.3.60 to pin.
ARG COMFYUI_VERSION=latest
ARG COMFYUI_REPO=https://github.com/comfyanonymous/ComfyUI.git
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128
ARG TORCH_VERSION=2.11.0
ARG TORCHVISION_VERSION=0.26.0
ARG TORCHAUDIO_VERSION=2.11.0
# Large CUDA wheels from pypi.nvidia.com stall often; be patient and retry.
ARG UV_HTTP_TIMEOUT=600
ARG UV_HTTP_RETRIES=10
ARG UV_CONCURRENT_DOWNLOADS=4

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_PREFER_BINARY=1 \
    PIP_NO_INPUT=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_CONSTRAINT=/opt/constraints.txt \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:${PATH}

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3.12 \
      python3.12-venv \
      git \
      ca-certificates \
      libgl1 \
      libglib2.0-0 \
      libsm6 \
      libxext6 \
      libxrender1 \
      ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/

# Constraints also apply to runtime custom-node installs so they can't swap out the CUDA torch build.
RUN printf '%s\n' \
      "torch==${TORCH_VERSION}" \
      "torchvision==${TORCHVISION_VERSION}" \
      "torchaudio==${TORCHAUDIO_VERSION}" \
      "transformers>=4.50.3,<5" \
      "huggingface-hub<1.0" \
      > /opt/constraints.txt \
    && install -d -o ubuntu -g ubuntu /opt/venv /comfyui /data

USER ubuntu

RUN uv venv --python /usr/bin/python3.12 /opt/venv

RUN set -eux; \
    if [ "${COMFYUI_VERSION}" = "latest" ]; then \
      COMFYUI_VERSION="$(git ls-remote --tags --refs --sort=-v:refname "${COMFYUI_REPO}" 'v*' \
        | head -n1 | sed 's#.*refs/tags/##')"; \
    fi; \
    git clone --depth 1 --branch "${COMFYUI_VERSION}" "${COMFYUI_REPO}" /comfyui; \
    echo "${COMFYUI_VERSION}" > /comfyui/VERSION

# torch goes first from the CUDA index so the bare `torch` in requirements.txt is already satisfied.
RUN --mount=type=cache,target=/home/ubuntu/.cache/uv,uid=1000,gid=1000 \
    uv pip install torch torchvision torchaudio --index-url "${TORCH_INDEX_URL}" \
    && uv pip install -r /comfyui/requirements.txt

# Fail the build if ComfyUI can't import its node graph (CPU only, no GPU needed).
RUN cd /comfyui && timeout 300 python main.py --quick-test-for-ci --cpu

# Install image-provided extensions; entrypoint seeds them into the data volume.
RUN git clone --depth 1 https://github.com/comfy-org/ComfyUI-Manager.git \
    /comfyui/custom_nodes/ComfyUI-Manager

COPY --chown=ubuntu:ubuntu --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh

WORKDIR /comfyui
VOLUME /data
EXPOSE 8188

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
