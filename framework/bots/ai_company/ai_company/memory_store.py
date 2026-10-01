"""Memoria de longo prazo por agente — agora em Postgres (schema memory.facts).

Cada agente partilha a tabela mas tem seu proprio namespace (coluna agent).
Interface async (asyncpg). Consumers chamam com await.

Fallback: se DATABASE_URL nao setada, retorna stub no-op que nao persiste nada.
"""
from __future__ import annotations

import os
from typing import Any

import asyncpg


class MemoryStore:
    def __init__(self, agent_name: str, database_url: str | None = None):
        self._agent = agent_name
        self._dsn = database_url or os.environ.get("DATABASE_URL")
        self._pool: asyncpg.Pool | None = None

    async def start(self) -> None:
        if not self._dsn:
            return
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=3)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def enabled(self) -> bool:
        return self._pool is not None

    async def save(self, *, key: str, value: str, tags: list[str] | None = None) -> dict:
        if self._pool is None:
            return {"action": "skipped", "key": key, "value": value, "tags": tags or []}
        tags = list(tags or [])
        row = await self._pool.fetchrow(
            """INSERT INTO memory.facts (agent, key, value, tags)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (agent, key) DO UPDATE SET
                 value = EXCLUDED.value, tags = EXCLUDED.tags, updated_at = now()
               RETURNING id, key, value, tags, EXTRACT(EPOCH FROM created_at)::int AS created_at,
                         EXTRACT(EPOCH FROM updated_at)::int AS updated_at,
                         CASE WHEN xmax::text::int > 0 THEN 'updated' ELSE 'created' END AS action""",
            self._agent, key, value, tags,
        )
        return dict(row)

    async def recall(self, query: str, limit: int = 5) -> list[dict]:
        if self._pool is None:
            return []
        import re
        tokens = re.findall(r"\w{2,}", (query or ""), re.UNICODE)
        if tokens:
            # FTS via to_tsquery com OR entre tokens
            ts_q = " | ".join(tokens)
            rows = await self._pool.fetch(
                """SELECT id, key, value, tags,
                          EXTRACT(EPOCH FROM created_at)::int AS created_at,
                          EXTRACT(EPOCH FROM updated_at)::int AS updated_at
                     FROM memory.facts
                    WHERE agent = $1
                      AND to_tsvector('simple', key || ' ' || value) @@ to_tsquery('simple', $2)
                    ORDER BY ts_rank(to_tsvector('simple', key || ' ' || value),
                                     to_tsquery('simple', $2)) DESC
                    LIMIT $3""",
                self._agent, ts_q, limit,
            )
        else:
            rows = await self._pool.fetch(
                """SELECT id, key, value, tags,
                          EXTRACT(EPOCH FROM created_at)::int AS created_at,
                          EXTRACT(EPOCH FROM updated_at)::int AS updated_at
                     FROM memory.facts
                    WHERE agent = $1
                    ORDER BY updated_at DESC LIMIT $2""",
                self._agent, limit,
            )
        return [dict(r) for r in rows]

    async def list_recent(self, limit: int = 20, tag: str | None = None) -> list[dict]:
        if self._pool is None:
            return []
        if tag:
            rows = await self._pool.fetch(
                """SELECT id, key, value, tags,
                          EXTRACT(EPOCH FROM created_at)::int AS created_at,
                          EXTRACT(EPOCH FROM updated_at)::int AS updated_at
                     FROM memory.facts
                    WHERE agent = $1 AND $2 = ANY(tags)
                    ORDER BY updated_at DESC LIMIT $3""",
                self._agent, tag, limit,
            )
        else:
            rows = await self._pool.fetch(
                """SELECT id, key, value, tags,
                          EXTRACT(EPOCH FROM created_at)::int AS created_at,
                          EXTRACT(EPOCH FROM updated_at)::int AS updated_at
                     FROM memory.facts
                    WHERE agent = $1
                    ORDER BY updated_at DESC LIMIT $2""",
                self._agent, limit,
            )
        return [dict(r) for r in rows]

    async def get(self, key: str) -> dict | None:
        if self._pool is None:
            return None
        r = await self._pool.fetchrow(
            """SELECT id, key, value, tags,
                      EXTRACT(EPOCH FROM created_at)::int AS created_at,
                      EXTRACT(EPOCH FROM updated_at)::int AS updated_at
                 FROM memory.facts WHERE agent = $1 AND key = $2""",
            self._agent, key,
        )
        return dict(r) if r else None

    async def delete(self, key: str) -> bool:
        if self._pool is None:
            return False
        status = await self._pool.execute(
            "DELETE FROM memory.facts WHERE agent = $1 AND key = $2", self._agent, key,
        )
        return status.endswith(" 1")

    async def edit(
        self,
        *,
        key: str,
        value: str | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        """Atualiza fato existente. Falha com KeyError se key nao existir.

        Pelo menos um de value/tags deve ser fornecido. Campos omitidos sao preservados.
        """
        if value is None and tags is None:
            raise ValueError("ao menos um de 'value' ou 'tags' deve ser fornecido")
        if self._pool is None:
            return {"action": "skipped", "key": key}
        sets: list[str] = []
        args: list[Any] = [self._agent, key]
        if value is not None:
            args.append(value)
            sets.append(f"value = ${len(args)}")
        if tags is not None:
            args.append(list(tags))
            sets.append(f"tags = ${len(args)}")
        sets.append("updated_at = now()")
        sql = (
            f"UPDATE memory.facts SET {', '.join(sets)} "
            "WHERE agent = $1 AND key = $2 "
            "RETURNING id, key, value, tags, "
            "EXTRACT(EPOCH FROM created_at)::int AS created_at, "
            "EXTRACT(EPOCH FROM updated_at)::int AS updated_at"
        )
        row = await self._pool.fetchrow(sql, *args)
        if row is None:
            raise KeyError(key)
        out = dict(row)
        out["action"] = "updated"
        return out

    async def count(self) -> int:
        if self._pool is None:
            return 0
        v = await self._pool.fetchval("SELECT COUNT(*) FROM memory.facts WHERE agent = $1", self._agent)
        return int(v or 0)
