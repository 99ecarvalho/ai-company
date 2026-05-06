"""Pool de conexoes Postgres compartilhado pro web + utilitarios."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import asyncpg


_pool: asyncpg.Pool | None = None


async def init_pool(dsn: str | None = None, min_size: int = 2, max_size: int = 10) -> asyncpg.Pool:
    """Cria o pool global. Chamado no lifespan do FastAPI."""
    global _pool
    if _pool is not None:
        return _pool
    dsn = dsn or os.environ["DATABASE_URL"]
    _pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    """Recupera o pool inicializado. Levanta se chamado antes de init_pool."""
    if _pool is None:
        raise RuntimeError("db pool nao inicializado (init_pool ainda nao rodou)")
    return _pool


@asynccontextmanager
async def connection():
    """Acquire connection do pool. Use em `async with connection() as conn: ...`."""
    async with pool().acquire() as conn:
        yield conn


async def fetch_one(query: str, *args) -> asyncpg.Record | None:
    async with connection() as conn:
        return await conn.fetchrow(query, *args)


async def fetch_all(query: str, *args) -> list[asyncpg.Record]:
    async with connection() as conn:
        return await conn.fetch(query, *args)


async def execute(query: str, *args) -> str:
    async with connection() as conn:
        return await conn.execute(query, *args)
