# Voice TTS container — Piper (https://github.com/rhasspy/piper)
# CPU-only, leve (~200MB image). Modelo PT-BR baixado no build.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIPER_VOICES_DIR=/voices

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Default voice: PT-BR feminina (faber-medium ~28MB). Override via env
# DEFAULT_VOICE+VOICE_URL pra trocar.
RUN mkdir -p ${PIPER_VOICES_DIR} \
    && wget -q -O ${PIPER_VOICES_DIR}/pt_BR-faber-medium.onnx \
        https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/faber/medium/pt_BR-faber-medium.onnx \
    && wget -q -O ${PIPER_VOICES_DIR}/pt_BR-faber-medium.onnx.json \
        https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/faber/medium/pt_BR-faber-medium.onnx.json

COPY server.py .

EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
