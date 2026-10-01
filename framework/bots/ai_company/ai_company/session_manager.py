"""Manages per-topic session directories.

Each active topic gets its own cwd under `sessions_root`:

    <sessions_root>/<topic_slug>/
      repos/          -> <workspace_repos>   (path ergonomics)
      company/        -> <workspace_company> (path ergonomics)
      .claude/
        agents/       # per-item symlinks (merge agent + main_repo)
        commands/     # per-item symlinks (merge agent + main_repo)
      .mcp-config.json  # written by ClaudeRunner on every invocation

`sessions_root` is passed explicitly (prod: /workspace/sessions, mounted
from the host at ${SESSIONS_DIR}/<agent>/ — D-51). The legacy default
`agent_home/sessions` is kept for tests/compat.

Claude's `session_id` (for `claude --resume` after a restart) is kept
in `messaging.conversations.claude_session_id` in Postgres — migration 009.
No filesystem dependency for persistent state.

The agent's identity (CLAUDE.md, knowledge, permissions, hooks, MCP config)
goes through other cwd-independent channels (system prompt, `--add-dir`, CLI
flags) — see D-61.

Subagents/commands are the exception: the claude CLI only scans `.claude/{agents,
commands}/` relative to the CWD, with no flag to redirect it. That is why we do
**per-item symlinks** inside the session_dir (D-64):
  - Agent: `<agent_home>/.claude/<subdir>/*`  (priority)
  - main_repo (optional): `<workspace_repos>/<main_repo>/.claude/<subdir>/*`
    acts as a fallback, without overriding the agent's items.

**Skills are not handled here** (D-105): they have a dedicated path via the global
symlink `~/.claude/skills -> /app/agents/<name>/skills/` in the entrypoint, cwd-independent.
The single source `agents/<name>/skills/` covers both creation via the MCP `save_skill`
(D-98) and skills committed by the human. `agents/`/`commands/` stay
in this merge because they have no MCP equivalent.

**Worktree CWD caveat:** when an agent has ONE `in_progress` worktree,
ClaudeRunner spawns with CWD = the worktree path (see
`ClaudeRunner._resolve_spawn_cwd`), not the session_dir. On that path the
symlinks from here are invisible — the claude CLI only sees the `.claude/` committed
in the worktree's repo. The agent's own and main_repo's subagents/commands
are discovered only when CWD = session_dir (no worktree in progress).
Skills, since they live in the global symlink, are visible from
any cwd.

GC removes directories with an mtime older than N hours (for the `.mcp-config.json`
and `.claude/` left on disk).
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

# .claude/ subfolders that are "plugin-style" — each item is standalone and
# can be merged per-item via symlink without structural conflicts. `skills`
# left this list in D-105 (dedicated path via a global symlink in the entrypoint,
# cwd-independent). `agents`/`commands` stay because they have no MCP equivalent.
CLAUDE_MERGE_SUBDIRS = ("agents", "commands")


class SessionManager:
    def __init__(
        self,
        agent_home: Path,
        workspace_repos: Path,
        workspace_company: Path,
        sessions_root: Path | None = None,
        db_pool=None,  # asyncpg.Pool | None — None accepted for unit tests without a DB.
        main_repo_name: str | None = None,
    ):
        self.agent_home = agent_home
        self.workspace_repos = workspace_repos
        self.workspace_company = workspace_company
        self.sessions_root = sessions_root if sessions_root is not None else (agent_home / SESSIONS_SUBDIR)
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self._pool = db_pool
        self.main_repo_name = main_repo_name
        # In-memory fallback when there is no pool (tests). Key = topic slug;
        # value = (claude_session_id, claude_session_cwd|None). Does not survive
        # a process restart — fine for tests; in prod the pool always exists.
        self._inmem_session_refs: dict[str, tuple[str, str | None]] = {}

    def topic_workdir(self, key: TopicKey) -> Path:
        return self.sessions_root / key.slug()

    def setup(self, key: TopicKey) -> Path:
        """Create the topic cwd with idempotent symlinks to repos/company."""
        wd = self.topic_workdir(key)
        wd.mkdir(parents=True, exist_ok=True)

        # Ergonomic symlinks: let agents reference "repos/<x>/..."
        # and "company/..." with relative paths. CLAUDE.md and knowledge do NOT live
        # here — they go through the system prompt / --add-dir (D-61).
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

        # bump mtime so it isn't GC'd while in use
        os.utime(wd, None)
        return wd

    def _merge_claude_subdirs(self, wd: Path) -> None:
        """Populate `<wd>/.claude/{agents,commands}/` via per-item symlinks.

        Sources, in order of precedence:
          1. `<agent_home>/.claude/<subdir>/*`  (agent wins)
          2. `<workspace_repos>/<main_repo>/.claude/<subdir>/*`  (optional)

        Idempotent: existing symlinks with the same target are kept; divergent ones
        are recreated (agent_home changes, main_repo changes, or an item was removed
        at the source). Items that no longer exist in any source are removed if they
        are managed symlinks.
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
                    hint="main_repo agents/commands will not be injected",
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
            # 1. Agent wins: populate first.
            src_agent = agent_claude / subdir
            if src_agent.is_dir():
                for item in src_agent.iterdir():
                    desired.setdefault(item.name, item)
            # 2. main_repo fills in the rest.
            if main_claude is not None:
                src_main = main_claude / subdir
                if src_main.is_dir():
                    for item in src_main.iterdir():
                        desired.setdefault(item.name, item)

            # Reconcile filesystem <-> desired.
            existing_names: set[str] = set()
            for entry in target_dir.iterdir():
                existing_names.add(entry.name)
                if entry.name not in desired:
                    # Stale item: only remove it if it is a managed symlink (never a dir
                    # written by the claude CLI).
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
        """Return `(session_id, cwd)` for a topic, or None if absent.

        `cwd` is the spawn_cwd that produced the session_id (migration 024). It can be
        None for ids stored before the migration — the runner treats that as "accept
        any cwd" for back-compat.

        Source of truth: `messaging.conversations.claude_session_id` +
        `claude_session_cwd`. Without a pool (tests), uses an in-memory dict.
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
        """Compat: returns only the session_id. Use `session_ref_for` when you want
        to validate the cwd before `--resume`."""
        ref = await self.session_ref_for(key)
        return ref[0] if ref else None

    async def clear_session_id(self, key: TopicKey) -> None:
        """Clear the topic's `claude_session_id` + `claude_session_cwd` (the next run
        spawns without `--resume`).

        Used by the runner when the CLI complains that the referenced session_id does not
        exist on disk (ghost session, D-70). A fresh start keeps semantic continuity
        via artifacts on disk; only the CLI conversation buffer is lost.
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
        """Persist `(session_id, cwd)` for a topic.

        `cwd` must be the spawn_cwd where claude ran (absolute path, str). The
        Claude CLI stores `<sid>.jsonl` in `~/.claude/projects/<encoded-cwd>/`,
        so resuming the id from another cwd produces a ghost session — the runner uses
        this field to check for a match before `--resume` (D-97). None is allowed
        for back-compat with legacy call sites.

        Without a pool (tests), uses an in-memory dict.
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
            # asyncpg returns a string like "UPDATE 1"
            if updated.endswith(" 0"):
                log.warning(
                    "session.save_no_row",
                    topic=key.slug(),
                    hint="conversation does not exist; session_id lost",
                )
        except Exception as e:
            log.warning("session.save_failed", err=str(e), topic=key.slug())

    def touch(self, key: TopicKey) -> None:
        wd = self.topic_workdir(key)
        if wd.exists():
            os.utime(wd, None)

    def gc(self, max_age_hours: float = 24.0) -> int:
        """Remove session directories idle for more than max_age_hours. Returns the number removed."""
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
