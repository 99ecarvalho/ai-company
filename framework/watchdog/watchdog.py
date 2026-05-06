"""Watchdog dos agentes.

Periodicamente (a cada POLL_INTERVAL_SEC), lista arquivos de heartbeat em
/heartbeats/*.txt e verifica se estao stale (ultima modificacao > STALE_THRESHOLD_SEC).

Se stale E nao esta em cooldown, restarta o container correspondente via
Docker SDK (/var/run/docker.sock).

Container name convention: agent-framework-agent-<name>-1

Env vars:
  HEARTBEAT_DIR          default /heartbeats
  POLL_INTERVAL_SEC      default 60
  STALE_THRESHOLD_SEC    default 180
  RESTART_COOLDOWN_SEC   default 600   (nao restarta mesmo agent por X segundos depois de restart)
  CONTAINER_PREFIX       default agent-framework-agent-
  CONTAINER_SUFFIX       default -1
  DRY_RUN                default ""    (setar a "1" pra so logar, nao restartar)
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import docker
import structlog


HEARTBEAT_DIR = Path(os.environ.get("HEARTBEAT_DIR", "/heartbeats"))
POLL_INTERVAL_SEC = float(os.environ.get("POLL_INTERVAL_SEC", "60"))
STALE_THRESHOLD_SEC = float(os.environ.get("STALE_THRESHOLD_SEC", "180"))
RESTART_COOLDOWN_SEC = float(os.environ.get("RESTART_COOLDOWN_SEC", "600"))
CONTAINER_PREFIX = os.environ.get("CONTAINER_PREFIX", "agent-framework-agent-")
CONTAINER_SUFFIX = os.environ.get("CONTAINER_SUFFIX", "-1")
DRY_RUN = bool(os.environ.get("DRY_RUN"))
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "8812"))

_stats = {"started_at": time.time(), "checks": 0, "restarts": 0, "last_check_at": None}


def _start_health_server() -> None:
    """Mini /health server em thread daemon. Mesma forma que orchestrator/health.py
    (duplicado pra watchdog nao depender de orchestrator/)."""
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    def status() -> dict:
        return {
            "status": "ok",
            "uptime_sec": int(time.time() - _stats["started_at"]),
            "checks": _stats["checks"],
            "restarts": _stats["restarts"],
            "last_check_at": _stats["last_check_at"],
            "dry_run": DRY_RUN,
        }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path != "/health":
                self.send_response(404); self.end_headers(); return
            body = _json.dumps(status()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a, **k):  # noqa: ARG002
            return

    server = HTTPServer(("0.0.0.0", HEALTH_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="health").start()


structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    logger_factory=structlog.PrintLoggerFactory(),
)
structlog.contextvars.bind_contextvars(component="watchdog")
log = structlog.get_logger("watchdog")


def main() -> int:
    client = docker.from_env()
    last_restart: dict[str, float] = {}

    _start_health_server()

    log.info(
        "watchdog.starting",
        heartbeat_dir=str(HEARTBEAT_DIR),
        poll_interval_sec=POLL_INTERVAL_SEC,
        stale_threshold_sec=STALE_THRESHOLD_SEC,
        restart_cooldown_sec=RESTART_COOLDOWN_SEC,
        dry_run=DRY_RUN,
        health_port=HEALTH_PORT,
    )

    while True:
        now = time.time()
        _stats["checks"] += 1
        _stats["last_check_at"] = now
        try:
            if not HEARTBEAT_DIR.exists():
                log.debug("watchdog.heartbeat_dir_missing")
            else:
                for f in HEARTBEAT_DIR.glob("*.txt"):
                    agent = f.stem
                    try:
                        ts = int(f.read_text(encoding="utf-8").strip())
                    except Exception:
                        ts = int(f.stat().st_mtime)
                    age = now - ts
                    if age < STALE_THRESHOLD_SEC:
                        continue
                    # Stale. Esta em cooldown?
                    cooldown_until = last_restart.get(agent, 0) + RESTART_COOLDOWN_SEC
                    if now < cooldown_until:
                        log.info(
                            "watchdog.stale_but_cooldown",
                            agent=agent,
                            age_sec=round(age, 1),
                            cooldown_remaining_sec=round(cooldown_until - now, 1),
                        )
                        continue
                    container_name = f"{CONTAINER_PREFIX}{agent}{CONTAINER_SUFFIX}"
                    log.warning(
                        "watchdog.agent_stale_restarting",
                        agent=agent,
                        age_sec=round(age, 1),
                        container=container_name,
                    )
                    if DRY_RUN:
                        log.info("watchdog.dry_run_skip_restart", container=container_name)
                    else:
                        try:
                            c = client.containers.get(container_name)
                            c.restart(timeout=10)
                            log.info("watchdog.restart_ok", container=container_name)
                            _stats["restarts"] += 1
                        except docker.errors.NotFound:
                            log.warning("watchdog.container_not_found", container=container_name)
                        except Exception:
                            log.exception("watchdog.restart_failed", container=container_name)
                    last_restart[agent] = now
        except Exception:
            log.exception("watchdog.loop_error")
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
