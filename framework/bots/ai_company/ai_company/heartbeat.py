"""Heartbeat for the watchdog.

Each agent periodically writes a file at
`/heartbeats/<agent_name>.txt` containing the unix timestamp. The
`watchdog` container reads these files and restarts agents whose heartbeat
is stale (not updated for more than N seconds) — a sign of an internal
deadlock (container alive but loop stuck). Container crashes are already
handled by compose's `restart: unless-stopped`.
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
    """Infinite loop — writes the timestamp every N seconds. Silent failures
    (e.g. dir without permission) do not bring the agent down."""
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
