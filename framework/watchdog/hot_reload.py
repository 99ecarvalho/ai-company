"""Hot reload de agentes em dev. Watcher de mtime em agent.yaml.

Mudanca detectada -> docker restart do container do agente.

Opt-in: rodar via `docker compose --profile dev up -d hot-reload`. Usa
mesma imagem do watchdog (ja tem docker SDK). Default off pra producao.

D-63: WATCH_FILES default reduzido a `agent.yaml` apenas. CLAUDE.md (e demais
secoes do system prompt) sao re-lidos pelo claude_runner a cada invocacao —
restart vira ruido. agent.yaml continua exigindo restart porque mexe em
permissions/MCP/pool no entrypoint. Pra restaurar comportamento antigo:
WATCH_FILES=CLAUDE.md,agent.yaml.

Env vars:
  AGENTS_DIR         default /workspace/agents
  POLL_SEC           default 5
  COOLDOWN_SEC       default 10  (debounce restart por agente)
  CONTAINER_PREFIX   default agent-framework-agent-
  CONTAINER_SUFFIX   default -1
  WATCH_FILES        default "agent.yaml" (csv)
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import docker
import structlog


AGENTS_DIR = Path(os.environ.get("AGENTS_DIR", "/workspace/agents"))
POLL_SEC = float(os.environ.get("POLL_SEC", "5"))
COOLDOWN_SEC = float(os.environ.get("COOLDOWN_SEC", "10"))
CONTAINER_PREFIX = os.environ.get("CONTAINER_PREFIX", "agent-framework-agent-")
CONTAINER_SUFFIX = os.environ.get("CONTAINER_SUFFIX", "-1")
WATCH_FILES = tuple(
    f.strip() for f in os.environ.get("WATCH_FILES", "agent.yaml").split(",")
    if f.strip()
)


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
structlog.contextvars.bind_contextvars(component="hot-reload")
log = structlog.get_logger("hot-reload")


def main() -> int:
    client = docker.from_env()
    last_mtime: dict[str, float] = {}
    last_restart: dict[str, float] = {}

    log.info(
        "hot_reload.starting",
        agents_dir=str(AGENTS_DIR),
        poll_sec=POLL_SEC,
        cooldown_sec=COOLDOWN_SEC,
        watch_files=list(WATCH_FILES),
    )

    while True:
        now = time.time()
        try:
            if AGENTS_DIR.exists():
                for agent_dir in AGENTS_DIR.iterdir():
                    if not agent_dir.is_dir():
                        continue
                    agent_name = agent_dir.name
                    for fname in WATCH_FILES:
                        f = agent_dir / fname
                        if not f.exists():
                            continue
                        mtime = f.stat().st_mtime
                        key = str(f)
                        prev = last_mtime.get(key)
                        last_mtime[key] = mtime
                        if prev is None or mtime <= prev:
                            continue
                        # mudanca detectada
                        cooldown_until = last_restart.get(agent_name, 0) + COOLDOWN_SEC
                        if now < cooldown_until:
                            log.info(
                                "hot_reload.cooldown_skip",
                                agent=agent_name, file=fname,
                                cooldown_remaining=round(cooldown_until - now, 1),
                            )
                            continue
                        container = f"{CONTAINER_PREFIX}{agent_name}{CONTAINER_SUFFIX}"
                        log.info(
                            "hot_reload.file_changed",
                            agent=agent_name, file=fname, container=container,
                        )
                        try:
                            c = client.containers.get(container)
                            c.restart(timeout=10)
                            log.info("hot_reload.restart_ok", container=container)
                        except docker.errors.NotFound:
                            log.warning("hot_reload.container_not_found", container=container)
                        except Exception:
                            log.exception("hot_reload.restart_failed", container=container)
                        last_restart[agent_name] = now
        except Exception:
            log.exception("hot_reload.loop_error")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
