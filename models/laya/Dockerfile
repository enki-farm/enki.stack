ARG BASE_IMAGE=nvcr.io/nvidia/pytorch:25.08-py3
FROM ${BASE_IMAGE}

ENV HF_HOME=/home/laya/.cache/huggingface \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && mkdir -p /home/laya/.cache/huggingface \
    && chown -R 1000:1000 /app /home/laya

COPY predictor.py .
RUN chown 1000:1000 predictor.py

USER 1000:1000
EXPOSE 8080

CMD ["python", "predictor.py"]