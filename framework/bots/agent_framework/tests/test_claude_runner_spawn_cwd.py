"""Tests do `ClaudeRunner._resolve_spawn_cwd` — escopo por topic da task (D-97).

Antes (D-61), o lookup era global por agent+in_progress: agente com 1 task
in_progress numa conv X via cwd da worktree vazar pra outras conversas
simultaneas. Agora s├│ usa worktree quando o topic eh `task-<slug>` E o slug
casa com a task in_progress do agente.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_framework.claude_runner import ClaudeRunner
from agent_framework.internal_client import TopicKey


class _FakeConn:
    def __init__(self, rows: list[dict[str, Any]]):
        # asyncpg Records aceitam subscript (`row["col"]`) e attr access; dicts
        # cobrem o caso de uso real do runner (`rows[0]["path"]`).
        self._rows = rows
        self.captured: tuple[str, tuple[Any, ...]] | None = None

    async def fetch(self, query: str, *args):
        self.captured = (query, args)
        return list(self._rows)

    async def fetchrow(self, query: str, *args):  # pragma: no cover
        return None


class _FakePool:
    def __init__(self, rows: list[dict[str, Any]]):
        self.conn = _FakeConn(rows)

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _make_runner(agent_name: str | None, pool: Any) -> ClaudeRunner:
    # SessionManager + BrokerClient exigidos pelo construtor mas nao usados
    # por `_resolve_spawn_cwd`. SimpleNamespace serve como stub trivial.
    return ClaudeRunner(
        session_mgr=SimpleNamespace(),  # type: ignore[arg-type]
        broker_client=SimpleNamespace(),  # type: ignore[arg-type]
        agent_name=agent_name,
        db_pool=pool,
    )


@pytest.mark.asyncio
async def test_topic_nao_eh_task_devolve_default(tmp_path: Path):
    """Topic ad-hoc (chat livre, ask, label-de-data) — sempre default_cwd,
    mesmo se o agente tem task in_progress com worktree."""
    # Pool retornaria worktree se fosse consultado — mas nao deve ser.
    pool = _FakePool([{"path": str(tmp_path / "worktree-da-outra-task")}])
    runner = _make_runner("product-owner", pool)

    default = tmp_path / "session-dir"
    default.mkdir()

    cwd = await runner._resolve_spawn_cwd(default, TopicKey("product-owner", "2026-04-27 16:23"))
    assert cwd == default
    # Confirma que nem chegou a consultar o DB.
    assert pool.conn.captured is None


@pytest.mark.asyncio
async def test_topic_task_com_worktree_propria_usa_worktree(tmp_path: Path):
    """`task-<slug>` + worktree linkada ao mesmo slug + agente como
    current_agent → cwd = worktree."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pool = _FakePool([{"path": str(worktree)}])
    runner = _make_runner("executor-core", pool)

    default = tmp_path / "default"
    default.mkdir()

    cwd = await runner._resolve_spawn_cwd(default, TopicKey("executor-core", "task-fix-bug-x"))
    assert cwd == worktree
    # Query foi feita filtrando pelo slug + agent.
    _, args = pool.conn.captured  # type: ignore[misc]
    assert args == ("fix-bug-x", "executor-core")


@pytest.mark.asyncio
async def test_topic_task_sem_worktree_correspondente_devolve_default(tmp_path: Path):
    """Topic `task-X` mas a task X nao tem worktree (ou nao eh do agente):
    query devolve 0 rows → default_cwd. NAO usa worktree de outra task in_progress."""
    pool = _FakePool([])  # task-fix-bug-x sem match (filtro slug+agent)
    runner = _make_runner("product-owner", pool)

    default = tmp_path / "default"
    default.mkdir()

    cwd = await runner._resolve_spawn_cwd(default, TopicKey("product-owner", "task-fix-bug-x"))
    assert cwd == default


@pytest.mark.asyncio
async def test_topic_task_path_inexistente_devolve_default(tmp_path: Path):
    """Worktree registrada no DB mas path sumiu do disco: defensivo, default."""
    pool = _FakePool([{"path": str(tmp_path / "nao-existe")}])
    runner = _make_runner("executor-core", pool)

    default = tmp_path / "default"
    default.mkdir()

    cwd = await runner._resolve_spawn_cwd(default, TopicKey("executor-core", "task-fix-bug-x"))
    assert cwd == default


@pytest.mark.asyncio
async def test_topic_task_multi_worktree_devolve_default(tmp_path: Path):
    """Task multi-repo (>1 worktrees pra mesmo slug) → ambiguo, default. Agente
    navega via paths absolutos com --add-dir."""
    w1 = tmp_path / "w1"; w1.mkdir()
    w2 = tmp_path / "w2"; w2.mkdir()
    pool = _FakePool([{"path": str(w1)}, {"path": str(w2)}])
    runner = _make_runner("executor-core", pool)

    default = tmp_path / "default"
    default.mkdir()

    cwd = await runner._resolve_spawn_cwd(default, TopicKey("executor-core", "task-multi-repo"))
    assert cwd == default


@pytest.mark.asyncio
async def test_sem_agent_name_ou_pool_devolve_default(tmp_path: Path):
    """Defensivo: agent_name None ou db_pool None → default sem consultar."""
    default = tmp_path / "default"
    default.mkdir()

    runner_no_agent = _make_runner(None, _FakePool([{"path": "/x"}]))
    assert await runner_no_agent._resolve_spawn_cwd(default, TopicKey("s", "task-x")) == default

    runner_no_pool = _make_runner("agent", None)
    assert await runner_no_pool._resolve_spawn_cwd(default, TopicKey("s", "task-x")) == default


@pytest.mark.asyncio
async def test_topic_task_slug_vazio_devolve_default(tmp_path: Path):
    """`task-` literal sem slug → default sem consultar."""
    pool = _FakePool([{"path": "/x"}])
    runner = _make_runner("agent", pool)
    default = tmp_path / "default"; default.mkdir()
    cwd = await runner._resolve_spawn_cwd(default, TopicKey("s", "task-"))
    assert cwd == default
    assert pool.conn.captured is None
