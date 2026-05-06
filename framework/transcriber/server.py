"""Transcriber HTTP service — faster-whisper + CUDA.

Endpoints:
  GET  /health           -> {"status":"ok","model":..., "device":...}
  POST /transcribe       -> multipart file upload, retorna {text, language, duration_ms, ...}

Config via env:
  WHISPER_MODEL           default "large-v3"  (tiny|base|small|medium|large-v2|large-v3|distil-large-v3)
  WHISPER_DEVICE          default "cuda"
  WHISPER_COMPUTE_TYPE    default "float16"   (float16|int8_float16|int8)
  WHISPER_LANGUAGE        default None        (auto-detect se vazio)
  WHISPER_CACHE_DIR       default "/cache/faster-whisper"
  HOST/PORT               default "0.0.0.0:8000"

Modelo eh carregado lazy (no primeiro /transcribe) pra permitir startup rapido
e falhas de GPU retornarem 500 em vez de quebrar o container. Download do
modelo acontece no primeiro uso e fica cacheado no volume /cache.
"""
from __future__ import annotations

import asyncio
import io
import os
import tempfile
import time
from pathlib import Path

import structlog
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse


# ---------- Config ----------

MODEL_NAME = os.environ.get("WHISPER_MODEL", "large-v3")
DEVICE = os.environ.get("WHISPER_DEVICE", "cuda")
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "float16")
DEFAULT_LANGUAGE = os.environ.get("WHISPER_LANGUAGE") or None
CACHE_DIR = os.environ.get("WHISPER_CACHE_DIR", "/cache/faster-whisper")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))


# ---------- Logging ----------

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(20),
    logger_factory=structlog.PrintLoggerFactory(),
)
structlog.contextvars.bind_contextvars(component="transcriber")
log = structlog.get_logger("transcriber")


# ---------- Model loading (lazy) ----------

_model = None
_model_lock = asyncio.Lock()


async def get_model():
    global _model
    if _model is not None:
        return _model
    async with _model_lock:
        if _model is not None:
            return _model
        log.info(
            "transcriber.loading_model",
            model=MODEL_NAME,
            device=DEVICE,
            compute_type=COMPUTE_TYPE,
            cache_dir=CACHE_DIR,
        )
        t0 = time.monotonic()
        from faster_whisper import WhisperModel

        Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
        _model = WhisperModel(
            MODEL_NAME,
            device=DEVICE,
            compute_type=COMPUTE_TYPE,
            download_root=CACHE_DIR,
        )
        log.info("transcriber.model_loaded", elapsed_sec=round(time.monotonic() - t0, 2))
    return _model


# ---------- App ----------

app = FastAPI(title="agent-framework transcriber", version="0.1.0")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "model": MODEL_NAME,
        "device": DEVICE,
        "compute_type": COMPUTE_TYPE,
        "model_loaded": _model is not None,
    }


@app.post("/transcribe")
async def transcribe(
    file: UploadFile = File(..., description="Audio file (qualquer formato suportado por ffmpeg)"),
    language: str | None = Form(default=None, description="pt|en|es|... ou vazio pra auto-detect"),
    vad_filter: bool = Form(default=True, description="Voice activity detection (filtra silencio)"),
    beam_size: int = Form(default=5),
):
    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Arquivo vazio.")
    size_kb = len(contents) / 1024.0

    # Escreve num temp file porque faster-whisper aceita path (melhor pra ffmpeg
    # auto-detectar formato). Mantem o sufixo pra hint de formato.
    suffix = Path(file.filename or "audio").suffix or ".bin"
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(contents)
            tmp_path = tmp.name
    except Exception as e:
        log.exception("transcriber.tmp_write_failed")
        raise HTTPException(status_code=500, detail=f"Falha escrevendo temp: {e}")

    try:
        model = await get_model()
    except Exception as e:
        log.exception("transcriber.model_load_failed")
        raise HTTPException(status_code=500, detail=f"Falha carregando modelo: {e}")

    lang = language or DEFAULT_LANGUAGE
    t0 = time.monotonic()

    def _run_transcription():
        segments, info = model.transcribe(
            tmp_path,
            language=lang,
            beam_size=beam_size,
            vad_filter=vad_filter,
        )
        # segments eh um generator — forca consumo pra medir duracao real
        segs = [
            {"start": s.start, "end": s.end, "text": s.text}
            for s in segments
        ]
        return segs, info

    try:
        segs, info = await asyncio.to_thread(_run_transcription)
    except Exception as e:
        log.exception("transcriber.transcribe_failed", filename=file.filename)
        raise HTTPException(status_code=500, detail=f"Falha na transcricao: {e}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    text = "".join(s["text"] for s in segs).strip()

    log.info(
        "transcriber.done",
        filename=file.filename,
        size_kb=round(size_kb, 1),
        language=info.language,
        language_prob=round(info.language_probability, 3),
        audio_duration_sec=round(info.duration, 2),
        elapsed_ms=elapsed_ms,
        segments=len(segs),
        rtf=round(elapsed_ms / 1000.0 / max(info.duration, 0.001), 3),
    )

    return JSONResponse({
        "text": text,
        "language": info.language,
        "language_probability": round(info.language_probability, 3),
        "audio_duration_sec": round(info.duration, 2),
        "elapsed_ms": elapsed_ms,
        "segments": segs,
    })


if __name__ == "__main__":
    import uvicorn

    log.info("transcriber.starting", host=HOST, port=PORT, model=MODEL_NAME, device=DEVICE)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
