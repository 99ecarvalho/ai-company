"""Heartbeat pro watchdog.

Cada agente escreve um arquivo periodicamente em
`/heartbeats/<agent_name>.txt` contendo o timestamp unix. O container
`watchdog` le esses arquivos e restarta agentes cujo heartbeat esta
stale (nao atualiza ha mais de N segundos) — sinal de deadlock interno
(container vivo mas loop travado). Crash de container ja eh resolvido
pelo `restart: unless-stopped` do compose.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from .log import get_logger


log = get_logger(__name__)


HEARTBEAT_DIR = Path(os.environ.get("HEARTBEAT_DIR", "/heartbeats"))
HEARTBEAT_INTERVAL_SEC = float(os.environ.get("HEARTBEAT_INTERVAL_SEC", "30"))


async def heartbeat_loop(agent_name: str) -> None:
    """Loop infinito — escreve timestamp a cada N segundos. Falha silenciosa
    (ex: dir sem permissao) nao derruba o agente."""
    try:
        HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        log.warning("heartbeat.mkdir_failed", dir=str(HEARTBEAT_DIR))
        return
    target = HEARTBEAT_DIR / f"{agent_name}.txt"
    log.info("heartbeat.starting", target=str(target), interval_sec=HEARTBEAT_INTERVAL_SEC)
    while True:
        try:
            target.write_text(str(int(time.time())), encoding="utf-8")
        except Exception as e:
            log.debug("heartbeat.write_failed", error=str(e))
        await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
