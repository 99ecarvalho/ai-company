# Transcriber: faster-whisper + CUDA + FastAPI.
#
# Serve como servico HTTP independente dos agentes Claude — qualquer agente
# pode invocar via POST /transcribe (multipart audio).
#
# Base nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04: traz CUDA + cuDNN 9
# compativel com ctranslate2 4.x (dependencia do faster-whisper 1.x).
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/cache/huggingface \
    XDG_CACHE_HOME=/cache

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install -r requirements.txt

COPY server.py .

# Volume pra cache de modelos (1-3GB por modelo — evita re-download)
VOLUME ["/cache"]

EXPOSE 8000

CMD ["python3", "-u", "server.py"]
