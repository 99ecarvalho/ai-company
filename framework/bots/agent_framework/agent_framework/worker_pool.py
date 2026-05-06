"""Pool simples: limita concorrencia de topics ativos via asyncio.Semaphore.

Nao temos "worker objects" — cada topic ativo adquire um slot enquanto
trabalha e solta ao ficar idle. Mais simples, menos bugs.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from .log import get_logger

log = get_logger(__name__)


class WorkerPool:
    def __init__(self, size: int):
        if size < 1:
            raise ValueError("WorkerPool size deve ser >= 1")
        self._sem = asyncio.Semaphore(size)
        self._size = size
        self._in_use = 0

    @property
    def size(self) -> int:
        return self._size

    @property
    def in_use(self) -> int:
        return self._in_use

    @property
    def free(self) -> int:
        return self._size - self._in_use

    @asynccontextmanager
    async def acquire(self, label: str = ""):
        """Contexto assincrono que adquire um slot, libera no exit."""
        await self._sem.acquire()
        self._in_use += 1
        log.info("pool.acquired", label=label, in_use=self._in_use, free=self.free)
        try:
            yield
        finally:
            self._in_use -= 1
            self._sem.release()
            log.info("pool.released", label=label, in_use=self._in_use, free=self.free)
