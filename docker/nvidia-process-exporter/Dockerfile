# Build for DGX Spark:
#   docker buildx build --platform linux/arm64 \
#     -t ghcr.io/enki-farm/nvidia-process-exporter:latest --push \
#     docker/nvidia-process-exporter

FROM python:3.12-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 exporter
COPY app.py .

USER 10001:10001
EXPOSE 9400

CMD ["python", "app.py"]