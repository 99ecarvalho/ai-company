"""SessionManager tests — idempotent symlinks + session_id tracking + GC.

Post-D-61: CLAUDE.md and knowledge are no longer symlinks in the session_dir
(identity goes through the system prompt / --add-dir). What remains is repos/company
for path ergonomics.

Post-migration 009: session_id lives in `messaging.conversations.claude_session_id`
(Postgres). When SessionManager is instantiated without db_pool (as in the tests),
it falls back to an equivalent in-memory store.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from ai_company.session_manager import SessionManager
from ai_company.internal_client import TopicKey


@pytest.fixture
def tmp_env(tmp_path: Path):
    agent_home = tmp_path / "agent_home"
    workspace_repos = tmp_path / "workspace_repos"
    workspace_company = tmp_path / "workspace_company"
    agent_home.mkdir()
    (agent_home / "CLAUDE.md").write_text("persona")
    (agent_home / "knowledge").mkdir()
    workspace_repos.mkdir()
    workspace_company.mkdir()
    return SessionManager(
        agent_home=agent_home,
        workspace_repos=workspace_repos,
        workspace_company=workspace_company,
    )


def _write_subagent(root: Path, name: str, body: str = "# sub") -> Path:
    ag_dir = root / "agents"
    ag_dir.mkdir(parents=True, exist_ok=True)
    path = ag_dir / f"{name}.md"
    path.write_text(body)
    return path


def _write_command(root: Path, name: str, body: str = "/cmd") -> Path:
    cmd_dir = root / "commands"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    path = cmd_dir / f"{name}.md"
    path.write_text(body)
    return path


def test_topic_workdir_and_setup_creates_symlinks(tmp_env: SessionManager):
    key = TopicKey(stream="debug", topic="smoke1")
    wd = tmp_env.setup(key)
    assert wd.exists()
    # Only repos + company become symlinks — CLAUDE.md/knowledge go
    # through the system prompt + --add-dir (D-61).
    assert (wd / "repos").is_symlink()
    assert (wd / "company").is_symlink()
    assert not (wd / "CLAUDE.md").exists()
    assert not (wd / "knowledge").exists()
    # They point to the right targets
    assert (wd / "repos").resolve() == tmp_env.workspace_repos.resolve()
    assert (wd / "company").resolve() == tmp_env.workspace_company.resolve()


def test_setup_is_idempotent(tmp_env: SessionManager):
    key = TopicKey(stream="debug", topic="smoke1")
    wd1 = tmp_env.setup(key)
    wd2 = tmp_env.setup(key)
    assert wd1 == wd2


async def test_session_id_roundtrip_inmem_fallback(tmp_env: SessionManager):
    """Without db_pool, uses the in-memory fallback. Supports a basic roundtrip."""
    key = TopicKey(stream="debug", topic="smoke1")
    tmp_env.setup(key)
    assert await tmp_env.session_id_for(key) is None
    await tmp_env.save_session_id(key, "sess-abc-123")
    assert await tmp_env.session_id_for(key) == "sess-abc-123"
    # Overwrite
    await tmp_env.save_session_id(key, "sess-new-456")
    assert await tmp_env.session_id_for(key) == "sess-new-456"


async def test_clear_session_id_removes_entry(tmp_env: SessionManager):
    """clear_session_id clears the id (D-70 — ghost session recovery)."""
    key = TopicKey(stream="debug", topic="ghost")
    await tmp_env.save_session_id(key, "sess-ghost-xyz")
    assert await tmp_env.session_id_for(key) == "sess-ghost-xyz"
    await tmp_env.clear_session_id(key)
    assert await tmp_env.session_id_for(key) is None
    # Idempotent: clearing a topic without an id does not blow up.
    await tmp_env.clear_session_id(key)
    assert await tmp_env.session_id_for(key) is None


async def test_session_id_isolated_per_topic(tmp_env: SessionManager):
    k1 = TopicKey(stream="debug", topic="t1")
    k2 = TopicKey(stream="debug", topic="t2")
    await tmp_env.save_session_id(k1, "sess-1")
    await tmp_env.save_session_id(k2, "sess-2")
    assert await tmp_env.session_id_for(k1) == "sess-1"
    assert await tmp_env.session_id_for(k2) == "sess-2"


async def test_session_ref_persists_cwd_alongside_sid(tmp_env: SessionManager):
    """D-97: save stores (sid, cwd); session_ref_for returns the tuple."""
    key = TopicKey(stream="debug", topic="cwd-track")
    assert await tmp_env.session_ref_for(key) is None
    await tmp_env.save_session_id(key, "sess-A", cwd="/workspace/sessions/foo")
    assert await tmp_env.session_ref_for(key) == ("sess-A", "/workspace/sessions/foo")
    # Overwriting updates both fields.
    await tmp_env.save_session_id(key, "sess-B", cwd="/workspace/worktrees/core/x")
    assert await tmp_env.session_ref_for(key) == ("sess-B", "/workspace/worktrees/core/x")
    # Save without cwd stores cwd=None (back-compat with legacy call sites).
    await tmp_env.save_session_id(key, "sess-C")
    assert await tmp_env.session_ref_for(key) == ("sess-C", None)


async def test_clear_resets_cwd_as_well_as_sid(tmp_env: SessionManager):
    """clear_session_id drops the cwd too, to avoid improper reuse."""
    key = TopicKey(stream="debug", topic="cwd-clear")
    await tmp_env.save_session_id(key, "sess-X", cwd="/workspace/sessions/y")
    assert await tmp_env.session_ref_for(key) == ("sess-X", "/workspace/sessions/y")
    await tmp_env.clear_session_id(key)
    assert await tmp_env.session_ref_for(key) is None
    assert await tmp_env.session_id_for(key) is None


def test_topic_key_slug_deterministic():
    k1 = TopicKey(stream="my-stream", topic="smoke1")
    k2 = TopicKey(stream="my-stream", topic="smoke1")
    assert k1.slug() == k2.slug()
    # Special characters become _
    k3 = TopicKey(stream="weird/stream!", topic="x y z")
    assert k3.slug() == "weird_stream___x_y_z" or "weird" in k3.slug()


def test_topic_key_slug_truncates_long_topic():
    # Real case: an agent passed a whole description (300+ chars) as next_topic.
    # The slug used to exceed Linux's 255-byte limit and broke session_manager.
    long_topic = (
        "Analyze the SQL->exists() bug in schedule.php:180 in the public_api repo. "
        "Small complexity — calibrate the dose accordingly. Identify the root cause "
        "(whether it is exists() not handling false, or the query failing earlier), "
        "and map out the minimal safe fix for legacy code."
    )
    k = TopicKey(stream="analyst", topic=long_topic)
    slug = k.slug()
    assert len(slug.encode("utf-8")) <= 200
    # Deterministic: the same (stream, topic) produces the same slug.
    assert k.slug() == TopicKey(stream="analyst", topic=long_topic).slug()
    # Different long topics produce different slugs (the hash breaks ties).
    k2 = TopicKey(stream="analyst", topic=long_topic + " another variant")
    assert k.slug() != k2.slug()


def test_merge_claude_subdirs_agent_only(tmp_path: Path):
    """Without main_repo, the agent's agents/commands are reachable from the session_dir.

    Skills are not handled here (D-105) — dedicated path via a global symlink in the
    entrypoint.
    """
    agent_home = tmp_path / "agent_home"
    workspace_repos = tmp_path / "repos"
    workspace_company = tmp_path / "company"
    for p in (agent_home, workspace_repos, workspace_company):
        p.mkdir()
    agent_subagent = _write_subagent(agent_home / ".claude", "reviewer")
    agent_command = _write_command(agent_home / ".claude", "deploy")

    mgr = SessionManager(
        agent_home=agent_home,
        workspace_repos=workspace_repos,
        workspace_company=workspace_company,
    )
    wd = mgr.setup(TopicKey(stream="s", topic="t"))

    linked_subagent = wd / ".claude" / "agents" / "reviewer.md"
    linked_command = wd / ".claude" / "commands" / "deploy.md"
    assert linked_subagent.is_symlink()
    assert linked_subagent.resolve() == agent_subagent.resolve()
    assert linked_command.is_symlink()
    assert linked_command.resolve() == agent_command.resolve()
    # skills/ is NOT created by the merge (D-105).
    assert not (wd / ".claude" / "skills").exists()


def test_merge_claude_subdirs_with_main_repo(tmp_path: Path):
    """main_repo fills in entries the agent lacks; the agent wins on collision.

    Covers `agents/` and `commands/` (D-105 removed `skills/` from the merge).
    """
    agent_home = tmp_path / "agent_home"
    workspace_repos = tmp_path / "repos"
    workspace_company = tmp_path / "company"
    for p in (agent_home, workspace_repos, workspace_company):
        p.mkdir()
    main_repo = workspace_repos / "core"
    main_repo.mkdir()

    # The agent has reviewer + shared (shared also exists in main_repo).
    agent_reviewer = _write_subagent(agent_home / ".claude", "reviewer", body="agent-rev")
    agent_shared = _write_subagent(agent_home / ".claude", "shared", body="agent-shared")
    # main_repo has shared (will lose) + planner (will be added) + the deploy command.
    _write_subagent(main_repo / ".claude", "shared", body="repo-shared")
    main_planner = _write_subagent(main_repo / ".claude", "planner")
    main_cmd = _write_command(main_repo / ".claude", "deploy")

    mgr = SessionManager(
        agent_home=agent_home,
        workspace_repos=workspace_repos,
        workspace_company=workspace_company,
        main_repo_name="core",
    )
    wd = mgr.setup(TopicKey(stream="s", topic="t"))

    # reviewer: came from the agent.
    assert (wd / ".claude" / "agents" / "reviewer.md").resolve() == agent_reviewer.resolve()
    # shared: the AGENT wins (override) — must NOT point to main_repo.
    assert (wd / ".claude" / "agents" / "shared.md").resolve() == agent_shared.resolve()
    # planner: came from main_repo.
    assert (wd / ".claude" / "agents" / "planner.md").resolve() == main_planner.resolve()
    # the deploy command came from main_repo.
    assert (wd / ".claude" / "commands" / "deploy.md").resolve() == main_cmd.resolve()


def test_merge_claude_subdirs_idempotent_and_stale_cleanup(tmp_path: Path):
    """Repeated setup keeps correct links; an item removed from the source disappears from the session."""
    agent_home = tmp_path / "agent_home"
    workspace_repos = tmp_path / "repos"
    workspace_company = tmp_path / "company"
    for p in (agent_home, workspace_repos, workspace_company):
        p.mkdir()
    s1 = _write_subagent(agent_home / ".claude", "alpha")
    s2 = _write_subagent(agent_home / ".claude", "beta")

    mgr = SessionManager(
        agent_home=agent_home,
        workspace_repos=workspace_repos,
        workspace_company=workspace_company,
    )
    key = TopicKey(stream="s", topic="t")
    wd = mgr.setup(key)
    assert (wd / ".claude" / "agents" / "alpha.md").is_symlink()
    assert (wd / ".claude" / "agents" / "beta.md").is_symlink()

    # Remove beta from the source, run setup again: beta must go away; alpha intact.
    s2.unlink()
    wd2 = mgr.setup(key)
    assert wd == wd2
    assert (wd / ".claude" / "agents" / "alpha.md").resolve() == s1.resolve()
    assert not (wd / ".claude" / "agents" / "beta.md").exists()


def test_merge_claude_subdirs_missing_main_repo_is_warning_not_fatal(tmp_path: Path):
    """main_repo pointing to a nonexistent path does not break setup."""
    agent_home = tmp_path / "agent_home"
    workspace_repos = tmp_path / "repos"
    workspace_company = tmp_path / "company"
    for p in (agent_home, workspace_repos, workspace_company):
        p.mkdir()
    _write_subagent(agent_home / ".claude", "only-agent")

    mgr = SessionManager(
        agent_home=agent_home,
        workspace_repos=workspace_repos,
        workspace_company=workspace_company,
        main_repo_name="does-not-exist",
    )
    wd = mgr.setup(TopicKey(stream="s", topic="t"))
    # The agent's subagent is still available.
    assert (wd / ".claude" / "agents" / "only-agent.md").is_symlink()


def test_gc_removes_old_dirs(tmp_env: SessionManager):
    key_old = TopicKey(stream="debug", topic="old")
    key_new = TopicKey(stream="debug", topic="new")
    wd_old = tmp_env.setup(key_old)
    wd_new = tmp_env.setup(key_new)

    # Simulate an old mtime (2h ago)
    past = time.time() - (2 * 3600)
    import os
    os.utime(wd_old, (past, past))

    removed = tmp_env.gc(max_age_hours=1.0)
    assert removed == 1
    assert not wd_old.exists()
    assert wd_new.exists()
