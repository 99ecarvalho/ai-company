"""Structured JSON logging via structlog.
Agents emit one JSON object per line on stdout; `docker compose logs` is the transport.
"""
from __future__ import annotations

import logging
import sys
import structlog


def setup_logging(level: str = "INFO", agent_name: str | None = None) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=log_level, format="%(message)s", stream=sys.stdout)

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    if agent_name:
        structlog.contextvars.bind_contextvars(agent=agent_name)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
