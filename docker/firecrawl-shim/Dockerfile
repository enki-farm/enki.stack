# syntax=docker/dockerfile:1
# Build multi-arch for DGX Spark and other cluster nodes:
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -t ghcr.io/enki-farm/firecrawl-shim:latest --push docker/firecrawl-shim

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 shim
COPY app.py .

USER 10001:10001
EXPOSE 3002

CMD ["uvicorn", "app:create_app", "--factory", "--host", "0.0.0.0", "--port", "3002", "--no-server-header"]
