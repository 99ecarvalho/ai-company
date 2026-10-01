"""Gerencia diretorios de sessao por topic.

Cada topic ativo ganha um cwd proprio sob `sessions_root`:

    <sessions_root>/<topic_slug>/
      repos/          -> <workspace_repos>   (ergonomia de path)
      company/        -> <workspace_company> (ergonomia de path)
      .claude/
        agents/       # per-item symlinks (merge agent + main_repo)
        commands/     # per-item symlinks (merge agent + main_repo)
      .mcp-config.json  # escrito pelo ClaudeRunner a cada invocacao

`sessions_root` e passado explicitamente (prod: /workspace/sessions, montado
do host em ${SESSIONS_DIR}/<agent>/ — D-51). Default legacy `agent_home/sessions`
preservado pra testes/compat.

O `session_id` do claude (pra `claude --resume` apos restart) e mantido
em `messaging.conversations.claude_session_id` no Postgres — migration 009.
Sem dependencia de filesystem pra state persistente.

Identidade do agente (CLAUDE.md, knowledge, permissions, hooks, MCP config)
vai por outros canais cwd-independentes (system prompt, `--add-dir`, CLI
flags) — ver D-61.

Subagents/commands sao a exceção: o claude CLI so varre `.claude/{agents,
commands}/` relativo ao CWD, sem flag pra redirecionar. Por isso fazemos
**per-item symlink** dentro do session_dir (D-64):
  - Agente: `<agent_home>/.claude/<subdir>/*`  (prioridade)
  - main_repo (opcional): `<workspace_repos>/<main_repo>/.claude/<subdir>/*`
    entra como fallback, sem sobrescrever items do agente.

**Skills nao entram aqui** (D-105): tem caminho dedicado via symlink global
`~/.claude/skills -> /app/agents/<name>/skills/` no entrypoint, cwd-independente.
Fonte unica `agents/<name>/skills/` cobre tanto criacao via MCP `save_skill`
(D-98) quanto skills committed pelo humano. `agents/`/`commands/` continuam
nesse merge porque nao tem equivalente MCP.

**Caveat do CWD worktree:** quando um executor tem UMA worktree `in_progress`,
o ClaudeRunner spawna com CWD = path da worktree (ver
`ClaudeRunner._resolve_spawn_cwd`), nao o session_dir. Nesse caminho os
symlinks daqui sao invisiveis — o claude CLI ve apenas o `.claude/` comitado
no repo da worktree. Subagents/commands proprios do agente e de main_repo
sao descobertos somente quando CWD = session_dir (não-executor, ou executor
fora de worktree). Skills, por viverem no symlink global, sao visiveis em
qualquer cwd.

GC remove diretorios com mtime mais velho que N horas (pra `.mcp-config.json`
e `.claude/` que ficam em disco).
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from .log import get_logger
from .internal_client import TopicKey

log = get_logger(__name__)

SESSIONS_SUBDIR = "sessions"

# Subpastas do .claude/ que sao "plugin-style" — cada item e standalone e
# pode ser mergeado per-item via symlink sem conflito estrutural. `skills`
# saiu daqui em D-105 (caminho dedicado via symlink global no entrypoint,
# cwd-independente). `agents`/`commands` ficam porque nao tem equivalente MCP.
CLAUDE_MERGE_SUBDIRS = ("agents", "commands")


class SessionManager:
    def __init__(
        self,
        agent_home: Path,
        workspace_repos: Path,
        workspace_company: Path,
        sessions_root: Path | None = None,
        db_pool=None,  # asyncpg.Pool | None — None aceito pra testes unitarios sem DB.
        main_repo_name: str | None = None,
    ):
        self.agent_home = agent_home
        self.workspace_repos = workspace_repos
        self.workspace_company = workspace_company
        self.sessions_root = sessions_root if sessions_root is not None else (agent_home / SESSIONS_SUBDIR)
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self._pool = db_pool
        self.main_repo_name = main_repo_name
        # Fallback em memoria quando nao ha pool (testes). Key = topic slug;
        # valor = (claude_session_id, claude_session_cwd|None). Nao sobrevive
        # a restart do processo — OK pra testes; em prod o pool sempre existe.
        self._inmem_session_refs: dict[str, tuple[str, str | None]] = {}

    def topic_workdir(self, key: TopicKey) -> Path:
        return self.sessions_root / key.slug()

    def setup(self, key: TopicKey) -> Path:
        """Cria o cwd do topic com symlinks idempotentes pra repos/company."""
        wd = self.topic_workdir(key)
        wd.mkdir(parents=True, exist_ok=True)

        # Symlinks de ergonomia: permitem agentes referenciarem "repos/<x>/..."
        # e "company/..." com paths relativos. CLAUDE.md e knowledge NAO ficam
        # aqui — vao pelo system prompt / --add-dir (D-61).
        links = {
            "repos": self.workspace_repos,
            "company": self.workspace_company,
        }
        for link_name, target in links.items():
            link_path = wd / link_name
            if link_path.is_symlink() or link_path.exists():
                continue
            try:
                link_path.symlink_to(target)
            except OSError as e:
                log.warning("session.symlink_failed", link=str(link_path), target=str(target), err=str(e))

        self._merge_claude_subdirs(wd)

        # atualiza mtime pra nao ser GC'd enquanto estiver em uso
        os.utime(wd, None)
        return wd

    def _merge_claude_subdirs(self, wd: Path) -> None:
        """Popula `<wd>/.claude/{agents,commands}/` via per-item symlinks.

        Fontes, em ordem de precedencia:
          1. `<agent_home>/.claude/<subdir>/*`  (agente vence)
          2. `<workspace_repos>/<main_repo>/.claude/<subdir>/*`  (opcional)

        Idempotente: symlinks existentes com target igual sao mantidos; divergentes
        sao refeitos (agent_home muda, main_repo muda, ou um item foi removido na
        fonte). Items que nao existem mais em nenhuma fonte sao removidos se forem
        symlinks geridos.
        """
        claude_root = wd / ".claude"
        try:
            claude_root.mkdir(exist_ok=True)
        except OSError as e:
            log.warning("session.claude_mkdir_failed", path=str(claude_root), err=str(e))
            return

        main_repo_root: Path | None = None
        if self.main_repo_name:
            candidate = self.workspace_repos / self.main_repo_name
            if candidate.is_dir():
                main_repo_root = candidate
            else:
                log.warning(
                    "session.main_repo_missing",
                    main_repo=self.main_repo_name,
                    expected=str(candidate),
                    hint="agents/commands do main_repo nao serao injetados",
                )

        agent_claude = self.agent_home / ".claude"
        main_claude = main_repo_root / ".claude" if main_repo_root else None

        for subdir in CLAUDE_MERGE_SUBDIRS:
            target_dir = claude_root / subdir
            try:
                target_dir.mkdir(exist_ok=True)
            except OSError as e:
                log.warning("session.claude_submkdir_failed", path=str(target_dir), err=str(e))
                continue

            desired: dict[str, Path] = {}
            # 1. Agente vence: popular primeiro.
            src_agent = agent_claude / subdir
            if src_agent.is_dir():
                for item in src_agent.iterdir():
                    desired.setdefault(item.name, item)
            # 2. main_repo preenche o que sobrou.
            if main_claude is not None:
                src_main = main_claude / subdir
                if src_main.is_dir():
                    for item in src_main.iterdir():
                        desired.setdefault(item.name, item)

            # Reconcilia filesystem <-> desired.
            existing_names: set[str] = set()
            for entry in target_dir.iterdir():
                existing_names.add(entry.name)
                if entry.name not in desired:
                    # Item stale: so removemos se for symlink gerido (nunca um dir
                    # escrito pelo claude CLI).
                    if entry.is_symlink():
                        try:
                            entry.unlink()
                        except OSError as e:
                            log.debug("session.claude_stale_unlink_failed",
                                      path=str(entry), err=str(e))
                    continue
                expected = desired[entry.name]
                if entry.is_symlink():
                    try:
                        current = os.readlink(entry)
                    except OSError:
                        current = None
                    if current != str(expected):
                        try:
                            entry.unlink()
                            entry.symlink_to(expected)
                        except OSError as e:
                            log.warning("session.claude_symlink_refresh_failed",
                                        path=str(entry), target=str(expected), err=str(e))

            for name, source in desired.items():
                if name in existing_names:
                    continue
                link_path = target_dir / name
                try:
                    link_path.symlink_to(source)
                except OSError as e:
                    log.warning("session.claude_symlink_failed",
                                link=str(link_path), target=str(source), err=str(e))

    async def session_ref_for(self, key: TopicKey) -> tuple[str, str | None] | None:
        """Retorna `(session_id, cwd)` pra um topic, ou None se ausente.

        `cwd` e o spawn_cwd que originou o session_id (migration 024). Pode ser
        None pra ids gravados antes da migration — runner trata como "aceita
        qualquer cwd" pra back-compat.

        Fonte de verdade: `messaging.conversations.claude_session_id` +
        `claude_session_cwd`. Sem pool (testes), usa dict em memoria.
        """
        if self._pool is None:
            return self._inmem_session_refs.get(key.slug())
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT c.claude_session_id, c.claude_session_cwd
                      FROM messaging.conversations c
                      JOIN messaging.streams s ON s.id = c.stream_id
                     WHERE s.name = $1 AND c.topic_name = $2
                    """,
                    key.stream, key.topic,
                )
        except Exception as e:
            log.warning("session.read_failed", err=str(e), topic=key.slug())
            return None
        if row is None or row["claude_session_id"] is None:
            return None
        return (row["claude_session_id"], row["claude_session_cwd"])

    async def session_id_for(self, key: TopicKey) -> str | None:
        """Compat: retorna so o session_id. Use `session_ref_for` quando quiser
        validar cwd antes do `--resume`."""
        ref = await self.session_ref_for(key)
        return ref[0] if ref else None

    async def clear_session_id(self, key: TopicKey) -> None:
        """Zera `claude_session_id` + `claude_session_cwd` do topic (proxima run
        spawna sem `--resume`).

        Usado pelo runner quando o CLI reclama que o session_id apontado nao
        existe em disco (ghost session, D-70). Fresh start preserva continuidade
        semantica via artefatos em disco; perde-se so o buffer da conversa CLI.
        """
        if self._pool is None:
            self._inmem_session_refs.pop(key.slug(), None)
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE messaging.conversations c
                       SET claude_session_id = NULL,
                           claude_session_cwd = NULL,
                           claude_session_used_at = NULL
                      FROM messaging.streams s
                     WHERE s.id = c.stream_id
                       AND s.name = $1
                       AND c.topic_name = $2
                    """,
                    key.stream, key.topic,
                )
        except Exception as e:
            log.warning("session.clear_failed", err=str(e), topic=key.slug())

    async def save_session_id(
        self, key: TopicKey, session_id: str, cwd: str | None = None
    ) -> None:
        """Persiste `(session_id, cwd)` pra um topic.

        `cwd` deve ser o spawn_cwd onde o claude rodou (path absoluto, str). O
        Claude CLI guarda `<sid>.jsonl` em `~/.claude/projects/<encoded-cwd>/`,
        entao retomar o id de outro cwd produz ghost session — o runner usa
        esse campo pra checar match antes de `--resume` (D-97). None permitido
        pra back-compat com call sites legados.

        Sem pool (testes), usa dict em memoria.
        """
        if self._pool is None:
            self._inmem_session_refs[key.slug()] = (session_id, cwd)
            return
        try:
            async with self._pool.acquire() as conn:
                updated = await conn.execute(
                    """
                    UPDATE messaging.conversations c
                       SET claude_session_id = $3,
                           claude_session_cwd = $4,
                           claude_session_used_at = now()
                      FROM messaging.streams s
                     WHERE s.id = c.stream_id
                       AND s.name = $1
                       AND c.topic_name = $2
                    """,
                    key.stream, key.topic, session_id, cwd,
                )
            # asyncpg retorna string tipo "UPDATE 1"
            if updated.endswith(" 0"):
                log.warning(
                    "session.save_no_row",
                    topic=key.slug(),
                    hint="conversa nao existe; session_id perdido",
                )
        except Exception as e:
            log.warning("session.save_failed", err=str(e), topic=key.slug())

    def touch(self, key: TopicKey) -> None:
        wd = self.topic_workdir(key)
        if wd.exists():
            os.utime(wd, None)

    def gc(self, max_age_hours: float = 24.0) -> int:
        """Remove diretorios de sessao idle a mais de max_age_hours. Retorna qtd removida."""
        if not self.sessions_root.exists():
            return 0
        cutoff = time.time() - (max_age_hours * 3600)
        removed = 0
        for entry in self.sessions_root.iterdir():
            if not entry.is_dir():
                continue
            if entry.stat().st_mtime < cutoff:
                log.info("session.gc", path=str(entry), mtime=entry.stat().st_mtime)
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        return removed
