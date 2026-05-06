"""Tests do SessionManager — symlinks idempotentes + session_id tracking + GC.

Pos-D-61: CLAUDE.md e knowledge nao ficam mais como symlinks no session_dir
(identidade vai pelo system prompt / --add-dir). Sobram repos/company como
ergonomia de path.

Pos-migracao 009: session_id vive em `messaging.conversations.claude_session_id`
(Postgres). Quando SessionManager e instanciado sem db_pool (caso dos testes),
cai num fallback em memoria equivalente.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent_framework.session_manager import SessionManager
from agent_framework.internal_client import TopicKey


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
    # Apenas repos + company viram symlinks — CLAUDE.md/knowledge vao
    # pelo system prompt + --add-dir (D-61).
    assert (wd / "repos").is_symlink()
    assert (wd / "company").is_symlink()
    assert not (wd / "CLAUDE.md").exists()
    assert not (wd / "knowledge").exists()
    # Apontam pros targets certos
    assert (wd / "repos").resolve() == tmp_env.workspace_repos.resolve()
    assert (wd / "company").resolve() == tmp_env.workspace_company.resolve()


def test_setup_is_idempotent(tmp_env: SessionManager):
    key = TopicKey(stream="debug", topic="smoke1")
    wd1 = tmp_env.setup(key)
    wd2 = tmp_env.setup(key)
    assert wd1 == wd2


async def test_session_id_roundtrip_inmem_fallback(tmp_env: SessionManager):
    """Sem db_pool, usa fallback em memoria. Aceita roundtrip basico."""
    key = TopicKey(stream="debug", topic="smoke1")
    tmp_env.setup(key)
    assert await tmp_env.session_id_for(key) is None
    await tmp_env.save_session_id(key, "sess-abc-123")
    assert await tmp_env.session_id_for(key) == "sess-abc-123"
    # Sobrescreve
    await tmp_env.save_session_id(key, "sess-new-456")
    assert await tmp_env.session_id_for(key) == "sess-new-456"


async def test_clear_session_id_removes_entry(tmp_env: SessionManager):
    """clear_session_id zera o id (D-70 — recovery de ghost session)."""
    key = TopicKey(stream="debug", topic="ghost")
    await tmp_env.save_session_id(key, "sess-ghost-xyz")
    assert await tmp_env.session_id_for(key) == "sess-ghost-xyz"
    await tmp_env.clear_session_id(key)
    assert await tmp_env.session_id_for(key) is None
    # Idempotente: clear de topic sem id nao explode.
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
    """D-97: save guarda (sid, cwd); session_ref_for devolve a tupla."""
    key = TopicKey(stream="debug", topic="cwd-track")
    assert await tmp_env.session_ref_for(key) is None
    await tmp_env.save_session_id(key, "sess-A", cwd="/workspace/sessions/foo")
    assert await tmp_env.session_ref_for(key) == ("sess-A", "/workspace/sessions/foo")
    # Sobrescrever atualiza ambos os campos.
    await tmp_env.save_session_id(key, "sess-B", cwd="/workspace/worktrees/core/x")
    assert await tmp_env.session_ref_for(key) == ("sess-B", "/workspace/worktrees/core/x")
    # Save sem cwd grava cwd=None (back-compat com call sites legados).
    await tmp_env.save_session_id(key, "sess-C")
    assert await tmp_env.session_ref_for(key) == ("sess-C", None)


async def test_clear_zera_cwd_alem_do_sid(tmp_env: SessionManager):
    """clear_session_id derruba cwd junto pra evitar reuso indevido."""
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
    # Caracteres especiais viram _
    k3 = TopicKey(stream="weird/stream!", topic="x y z")
    assert k3.slug() == "weird_stream___x_y_z" or "weird" in k3.slug()


def test_topic_key_slug_truncates_long_topic():
    # Caso real: coordenador passou descrição inteira (300+ chars) como next_topic.
    # Antes o slug estourava o limite de 255 bytes do Linux e quebrava session_manager.
    long_topic = (
        "Analise o bug SQL->exists() em schedule.php:180 no repo public_api. "
        "Complexidade pequena — calibre dose accordingly. Identifique a causa raiz "
        "(se é o exists() que não trata false, ou a query que falha antes), "
        "e mapeie o fix mínimo seguro para código legado."
    )
    k = TopicKey(stream="analista", topic=long_topic)
    slug = k.slug()
    assert len(slug.encode("utf-8")) <= 200
    # Determinístico: mesma (stream, topic) produz mesmo slug.
    assert k.slug() == TopicKey(stream="analista", topic=long_topic).slug()
    # Topics longos diferentes produzem slugs diferentes (hash desempata).
    k2 = TopicKey(stream="analista", topic=long_topic + " outra variante")
    assert k.slug() != k2.slug()


def test_merge_claude_subdirs_agent_only(tmp_path: Path):
    """Sem main_repo, agents/commands do agente ficam acessiveis no session_dir.

    Skills nao entram aqui (D-105) — caminho dedicado via symlink global no
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
    # skills/ NAO eh criado pelo merge (D-105).
    assert not (wd / ".claude" / "skills").exists()


def test_merge_claude_subdirs_with_main_repo(tmp_path: Path):
    """main_repo preenche entradas que o agente nao tem; agente vence em colisao.

    Cobre `agents/` e `commands/` (D-105 tirou `skills/` do merge).
    """
    agent_home = tmp_path / "agent_home"
    workspace_repos = tmp_path / "repos"
    workspace_company = tmp_path / "company"
    for p in (agent_home, workspace_repos, workspace_company):
        p.mkdir()
    main_repo = workspace_repos / "core"
    main_repo.mkdir()

    # Agente tem reviewer + shared (shared tambem existe no main_repo).
    agent_reviewer = _write_subagent(agent_home / ".claude", "reviewer", body="agent-rev")
    agent_shared = _write_subagent(agent_home / ".claude", "shared", body="agent-shared")
    # main_repo tem shared (vai perder) + planner (vai entrar) + comando deploy.
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

    # reviewer: veio do agente.
    assert (wd / ".claude" / "agents" / "reviewer.md").resolve() == agent_reviewer.resolve()
    # shared: AGENTE vence (override) — NAO deve apontar pro main_repo.
    assert (wd / ".claude" / "agents" / "shared.md").resolve() == agent_shared.resolve()
    # planner: veio do main_repo.
    assert (wd / ".claude" / "agents" / "planner.md").resolve() == main_planner.resolve()
    # command deploy veio do main_repo.
    assert (wd / ".claude" / "commands" / "deploy.md").resolve() == main_cmd.resolve()


def test_merge_claude_subdirs_idempotent_and_stale_cleanup(tmp_path: Path):
    """Setup repetido preserva links corretos; item removido da fonte some do session."""
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

    # Remove beta da fonte, roda setup de novo: beta deve sumir; alpha intacto.
    s2.unlink()
    wd2 = mgr.setup(key)
    assert wd == wd2
    assert (wd / ".claude" / "agents" / "alpha.md").resolve() == s1.resolve()
    assert not (wd / ".claude" / "agents" / "beta.md").exists()


def test_merge_claude_subdirs_missing_main_repo_is_warning_not_fatal(tmp_path: Path):
    """main_repo apontando pra path inexistente nao quebra setup."""
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
        main_repo_name="nao-existe",
    )
    wd = mgr.setup(TopicKey(stream="s", topic="t"))
    # Subagent do agente continua disponivel.
    assert (wd / ".claude" / "agents" / "only-agent.md").is_symlink()


def test_gc_removes_old_dirs(tmp_env: SessionManager):
    key_old = TopicKey(stream="debug", topic="old")
    key_new = TopicKey(stream="debug", topic="new")
    wd_old = tmp_env.setup(key_old)
    wd_new = tmp_env.setup(key_new)

    # Simula mtime antigo (2h atras)
    past = time.time() - (2 * 3600)
    import os
    os.utime(wd_old, (past, past))

    removed = tmp_env.gc(max_age_hours=1.0)
    assert removed == 1
    assert not wd_old.exists()
    assert wd_new.exists()
