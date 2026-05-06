FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir structlog==24.4.0 requests==2.32.3 docker==7.1.0

WORKDIR /app
COPY watchdog.py hot_reload.py ./

CMD ["python3", "-u", "watchdog.py"]
