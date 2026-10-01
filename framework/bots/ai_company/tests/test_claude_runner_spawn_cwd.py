"""Tests for `ClaudeRunner._resolve_spawn_cwd` — scoped by the task's topic (D-97).

Before (D-61), the lookup was global per agent+in_progress: an agent with 1 task
in_progress in conv X saw the worktree cwd leak into other concurrent
conversations. Now the worktree is only used when the topic is `task-<slug>` AND the slug
matches the agent's in_progress task.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ai_company.claude_runner import ClaudeRunner
from ai_company.internal_client import TopicKey


class _FakeConn:
    def __init__(self, rows: list[dict[str, Any]]):
        # asyncpg Records support subscript (`row["col"]`) and attr access; dicts
        # cover the runner's real use case (`rows[0]["path"]`).
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
    # SessionManager + BrokerClient are required by the constructor but not used
    # by `_resolve_spawn_cwd`. SimpleNamespace works as a trivial stub.
    return ClaudeRunner(
        session_mgr=SimpleNamespace(),  # type: ignore[arg-type]
        broker_client=SimpleNamespace(),  # type: ignore[arg-type]
        agent_name=agent_name,
        db_pool=pool,
    )


@pytest.mark.asyncio
async def test_non_task_topic_returns_default(tmp_path: Path):
    """Ad-hoc topic (free chat, ask, date label) — always default_cwd,
    even if the agent has an in_progress task with a worktree."""
    # The pool would return a worktree if queried — but it must not be.
    pool = _FakePool([{"path": str(tmp_path / "worktree-of-other-task")}])
    runner = _make_runner("product-owner", pool)

    default = tmp_path / "session-dir"
    default.mkdir()

    cwd = await runner._resolve_spawn_cwd(default, TopicKey("product-owner", "2026-04-27 16:23"))
    assert cwd == default
    # Confirm it did not even query the DB.
    assert pool.conn.captured is None


@pytest.mark.asyncio
async def test_task_topic_with_own_worktree_uses_worktree(tmp_path: Path):
    """`task-<slug>` + worktree linked to the same slug + agent as
    current_agent → cwd = worktree."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pool = _FakePool([{"path": str(worktree)}])
    runner = _make_runner("executor-core", pool)

    default = tmp_path / "default"
    default.mkdir()

    cwd = await runner._resolve_spawn_cwd(default, TopicKey("executor-core", "task-fix-bug-x"))
    assert cwd == worktree
    # The query filtered by slug + agent.
    _, args = pool.conn.captured  # type: ignore[misc]
    assert args == ("fix-bug-x", "executor-core")


@pytest.mark.asyncio
async def test_task_topic_without_matching_worktree_returns_default(tmp_path: Path):
    """Topic `task-X` but task X has no worktree (or does not belong to the agent):
    the query returns 0 rows → default_cwd. Does NOT use another in_progress task's worktree."""
    pool = _FakePool([])  # task-fix-bug-x has no match (slug+agent filter)
    runner = _make_runner("product-owner", pool)

    default = tmp_path / "default"
    default.mkdir()

    cwd = await runner._resolve_spawn_cwd(default, TopicKey("product-owner", "task-fix-bug-x"))
    assert cwd == default


@pytest.mark.asyncio
async def test_task_topic_missing_path_returns_default(tmp_path: Path):
    """Worktree registered in the DB but its path is gone from disk: defensive, default."""
    pool = _FakePool([{"path": str(tmp_path / "does-not-exist")}])
    runner = _make_runner("executor-core", pool)

    default = tmp_path / "default"
    default.mkdir()

    cwd = await runner._resolve_spawn_cwd(default, TopicKey("executor-core", "task-fix-bug-x"))
    assert cwd == default


@pytest.mark.asyncio
async def test_task_topic_multi_worktree_returns_default(tmp_path: Path):
    """Multi-repo task (>1 worktrees for the same slug) → ambiguous, default. The agent
    navigates via absolute paths with --add-dir."""
    w1 = tmp_path / "w1"; w1.mkdir()
    w2 = tmp_path / "w2"; w2.mkdir()
    pool = _FakePool([{"path": str(w1)}, {"path": str(w2)}])
    runner = _make_runner("executor-core", pool)

    default = tmp_path / "default"
    default.mkdir()

    cwd = await runner._resolve_spawn_cwd(default, TopicKey("executor-core", "task-multi-repo"))
    assert cwd == default


@pytest.mark.asyncio
async def test_without_agent_name_or_pool_returns_default(tmp_path: Path):
    """Defensive: agent_name None or db_pool None → default without querying."""
    default = tmp_path / "default"
    default.mkdir()

    runner_no_agent = _make_runner(None, _FakePool([{"path": "/x"}]))
    assert await runner_no_agent._resolve_spawn_cwd(default, TopicKey("s", "task-x")) == default

    runner_no_pool = _make_runner("agent", None)
    assert await runner_no_pool._resolve_spawn_cwd(default, TopicKey("s", "task-x")) == default


@pytest.mark.asyncio
async def test_task_topic_empty_slug_returns_default(tmp_path: Path):
    """Literal `task-` without a slug → default without querying."""
    pool = _FakePool([{"path": "/x"}])
    runner = _make_runner("agent", pool)
    default = tmp_path / "default"; default.mkdir()
    cwd = await runner._resolve_spawn_cwd(default, TopicKey("s", "task-"))
    assert cwd == default
    assert pool.conn.captured is None
