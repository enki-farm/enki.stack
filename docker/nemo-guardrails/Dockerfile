# syntax=docker/dockerfile:1
# Build multi-arch for DGX Spark and other cluster nodes:
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -t ghcr.io/enki-farm/nemo-server:latest --push docker/nemo-guardrails

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NEMO_GUARDRAILS_NO_USAGE_STATS=1 \
    DO_NOT_TRACK=1

RUN pip install --no-cache-dir "nemoguardrails[server]==0.24.1" \
    && useradd --create-home --uid 10001 guardrails

WORKDIR /app
USER 10001:10001
EXPOSE 8000

CMD ["nemoguardrails", "server", "--config", "/app/config", "--default-config-id", "default", "--port", "8000"]