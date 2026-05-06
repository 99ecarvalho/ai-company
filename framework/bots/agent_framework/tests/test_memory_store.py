"""Tests do MemoryStore — paths sem Postgres (no-pool) e validacao de edit."""
from __future__ import annotations

import pytest

from agent_framework.memory_store import MemoryStore


def _store_without_pool() -> MemoryStore:
    # Sem DATABASE_URL e sem start() -> _pool fica None, metodos viram stubs.
    return MemoryStore(agent_name="tester", database_url="")


async def test_delete_returns_false_without_pool():
    store = _store_without_pool()
    assert await store.delete("x") is False


async def test_edit_raises_without_args():
    store = _store_without_pool()
    with pytest.raises(ValueError):
        await store.edit(key="x")


async def test_edit_without_pool_returns_skipped():
    store = _store_without_pool()
    out = await store.edit(key="x", value="v")
    assert out["action"] == "skipped"
