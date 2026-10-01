"""Wrapper around the claude CLI.

Spawns the subprocess `claude -p <prompt> --output-format stream-json --verbose`,
runs it with cwd = the topic directory, persists session_id for a future --resume,
and passes `--mcp-config` pointing to the in-process MCP server (Phase 1c).
The answer goes back to the broker as a new message (the broker does not support
editing the "thinking..." ack); the ack stays as history.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import yaml

from .log import get_logger
from .mcp.broker import McpBroker
from .memory_store import MemoryStore
from .session_manager import SessionManager
from .internal_client import TopicKey, InternalClient as BrokerClient

log = get_logger(__name__)

# Regex to strip @-mentions of the bot itself from the user's text
MENTION_RE = re.compile(r"@\*\*[^*]+\*\*\s*")

# MCP server name in --mcp-config (claude uses it as the tool namespace)
MCP_SERVER_NAME = "ai_company"

# rc that indicates an external kill (SIGKILL=137, SIGTERM=143). Retriable.
RETRIABLE_RC = {137, 143}
MAX_ATTEMPTS = 2      # 1 original attempt + 1 retry
RETRY_BACKOFF_SEC = 5.0

# Pattern emitted by the Claude CLI when `--resume X` points to a session_id
# that does not exist on disk. Our runner stores the new session_id as soon as the
# CLI emits `system/init`, but if the run dies quickly the CLI may not have time
# to persist the fork's .jsonl — we end up with a ghost id in the DB that blows up
# on the next attempt. Detect → clear session_id + retry without `--resume`
# (D-70).
GHOST_SESSION_RE = re.compile(r"No conversation found with session ID:\s*([a-f0-9-]+)")

# Handoff body posted by the reactor (orchestrator/reactor.py:_handoff_body).
# Captures task_slug + the step taken over. Used in handle() to detect stale
# handoffs (task already in a terminal status or phase already completed) — when the
# reactor posts a handoff during a run that already absorbed the phase via --resume
# continuity, the message sits in the dispatcher queue and would be processed in an
# extra spawn, producing a "late, already done" answer (pure waste).
# Observed symptom: 2026-05-05 fix-task-16 (2 cascade runs after `done`).
HANDOFF_BODY_RE = re.compile(
    r"^➡️ \*\*Handoff from.*?\*\*Task:\*\* `([a-z0-9][a-z0-9-]*)` — you take over phase \*\*([a-z0-9_-]+)\*\*",
    re.S,
)

# Directory where the Claude CLI persists each session's jsonl. Layout:
#   ~/.claude/projects/<cwd-encoded>/<session_id>.jsonl
# The per-cwd subdir is derived by the CLI, so we scan all of them.
_CLAUDE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"

# Per-line buffer for the Claude CLI's stream-json. The asyncio default is 64 KiB,
# exceeded every time a large tool_use_result (Read of a ~40-50 KB file
# with line numbers + JSON escaping, Bash with long output, etc.) lands
# on a single line. We raise it to 16 MiB; larger lines are treated as
# oversized and the turn is retried with a recovery prompt asking for partial reads.
STREAM_STDOUT_LIMIT_BYTES = 16 * 1024 * 1024

# Prompt used on the retry after an oversized line. Sent as a new turn via
# `--resume`, asking Claude to redo the step in slices.
OVERSIZED_RECOVERY_PROMPT = (
    "The previous turn was interrupted by the framework: a single line of "
    "Claude's stream-json (typically the result of a tool_use like "
    "Read/Bash/Grep) exceeded the internal 16 MiB limit. The response was "
    "discarded.\n\n"
    "Please redo the previous step using **partial reads**, so you don't "
    "blow the buffer again:\n"
    "- `Read`: pass `offset` + `limit` (paginate in chunks of ~200-500 lines).\n"
    "- `Bash`: prefer `head -n`, `tail -n`, `sed -n 'A,Bp'`, `grep -n ... | "
    "head`, `wc -l`. Avoid `cat` on large files.\n"
    "- `Grep`: use `-n` with a small `-C` instead of full dumps.\n\n"
    "Continue from where you stopped and deliver the same final answer."
)

# D-63: instance sections (CONTEXT, philosophy, agent CLAUDE.md, Team block)
# are editable via the PWA. platform.md is a framework invariant — it lives in
# /app/system_prompts/platform.md (COPY in the agent Dockerfile) and is not
# editable by the instance. claude_runner re-reads everything on every invocation —
# edits apply to the next task, without a restart.
_SYSTEM_PROMPTS_DIR = Path("/workspace/company/system_prompts")
_PLATFORM_PROMPT_PATH = Path("/app/system_prompts/platform.md")
_SYSTEM_PROMPTS_CONFIG_PATH = _SYSTEM_PROMPTS_DIR / "config.yaml"
_WORKFLOWS_YAML_PATH = Path("/workspace/company/workflows.yaml")

# Defaults applied if config.yaml is missing or a key is absent — all sections
# ON. Reconcile copies config.yaml.example on first setup, so in prod
# the file always exists; defaults are the last fallback.
_SYSTEM_PROMPT_TOGGLE_DEFAULTS: dict[str, bool] = {
    "include_platform_prompt": True,
    "include_company_context": True,
    "include_company_philosophy": True,
    "include_agent_claude_md": True,
    "include_team_block": True,
    "include_invocation_context": True,
    "include_task_state": True,
    "include_step_instructions": True,
}


def _load_system_prompt_toggles() -> dict[str, bool]:
    """Read the system prompts' config.yaml and return a dict of toggles.

    IO/parse failures fall back to the default (all ON) with a warning — we prefer
    not to break runs because of a corrupt config. The user edits it via the PWA or
    directly in the file; a read failure = pre-D-63 behavior.
    """
    toggles = dict(_SYSTEM_PROMPT_TOGGLE_DEFAULTS)
    try:
        raw = _SYSTEM_PROMPTS_CONFIG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return toggles
    except Exception as e:
        log.warning("runner.system_prompt_config_read_failed", err=str(e))
        return toggles
    try:
        data = yaml.safe_load(raw) or {}
    except Exception as e:
        log.warning("runner.system_prompt_config_parse_failed", err=str(e))
        return toggles
    if not isinstance(data, dict):
        log.warning("runner.system_prompt_config_invalid", got=type(data).__name__)
        return toggles
    for key in toggles:
        v = data.get(key)
        if isinstance(v, bool):
            toggles[key] = v
    return toggles


@dataclass
class RunOutcome:
    ok: bool
    rc: int | None
    result_text: str = ""
    session_id: str | None = None
    error_subtype: str | None = None
    stderr: str = ""
    total_cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    usage: dict | None = None
    tool_uses: list[str] = field(default_factory=list)
    # How many stream-json lines were dropped for exceeding
    # STREAM_STDOUT_LIMIT_BYTES. >0 signals that the parser skipped content;
    # if the final `result` did not arrive, the turn is retriable with the recovery prompt.
    oversized_lines_skipped: int = 0
    # The Claude CLI complained that `--resume <sid>` points to a nonexistent session
    # (ghost session id from a fork that was never written to disk). The attempt
    # loop uses this to clear the DB and retry without `--resume` without spending
    # a normal attempt (D-70).
    ghost_session: bool = False

    @property
    def retriable(self) -> bool:
        # External kill: container was restarted or OOM
        if self.rc in RETRIABLE_RC:
            return True
        # Failure without any partial answer — probably an interruption
        if (self.rc or 0) != 0 and not self.result_text:
            return True
        # The parser skipped huge line(s) and could not build a final answer:
        # retry with the recovery prompt asking for partial reads.
        if self.oversized_lines_skipped and not self.result_text:
            return True
        return False


_COMPANY_CONTEXT_PATH = Path("/workspace/company/CONTEXT.md")
_COMPANY_PHILOSOPHY_PATH = Path("/workspace/company/philosophy.md")
_COMPANY_FILE_CAP_BYTES = 8 * 1024  # cap for what is injected into the system prompt


def _read_capped(path: Path, cap: int = _COMPANY_FILE_CAP_BYTES) -> str:
    """Read a file + truncate it if it exceeds the cap. Empty if it does not exist."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    enc = text.encode("utf-8")
    if len(enc) <= cap:
        return text
    return enc[:cap].decode("utf-8", errors="ignore") + "\n\n_(truncated)_"


def _auth_headers() -> dict[str, str]:
    """Bearer with BROKER_TOKEN for the web's authenticated endpoints (telemetry/
    live-event). Post-D-95 the web no longer has a dev bypass, so posts without
    the header return 401 and the tool_uses/thinkings disappear from the PWA."""
    tok = os.environ.get("BROKER_TOKEN")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _session_jsonl_exists(session_id: str) -> bool:
    """True if the Claude CLI has already persisted `<session_id>.jsonl` to disk.

    The CLI emits the new session_id in the first `system/init` event, but only
    writes the jsonl as the turn progresses. If the run dies early (e.g. init
    error, quick SIGKILL, resume pointing to a ghost), the id exists in the stream
    but never touches the filesystem. Using this to decide whether to persist it in the
    DB avoids ghost loops where the next `--resume` blows up with `No conversation
    found with session ID`.
    """
    if not session_id:
        return False
    root = _CLAUDE_PROJECTS_ROOT
    if not root.is_dir():
        return False
    # One project per cwd — we walk all of them. The directory is small (1 per topic),
    # stat is cheap; not worth caching.
    target = f"{session_id}.jsonl"
    for proj_dir in root.iterdir():
        if not proj_dir.is_dir():
            continue
        if (proj_dir / target).is_file():
            return True
    return False


class ClaudeRunner:
    def __init__(
        self,
        session_mgr: SessionManager,
        broker_client: BrokerClient,
        broker: McpBroker | None = None,
        mcp_url_for: Any = None,  # callable: slug -> url
        allowed_tools: list[str] | None = None,
        telemetry_url: str | None = None,
        agent_name: str | None = None,
        memory: MemoryStore | None = None,
        memory_auto_inject_limit: int = 0,
        model: str | None = None,
        effort: str | None = None,
        db_pool: Any = None,  # asyncpg.Pool | None — used to build the "## Team" block
    ):
        self.session_mgr = session_mgr
        self.broker_client = broker_client
        self.broker = broker
        self.mcp_url_for = mcp_url_for
        # Allow all of the framework's MCP tools by default
        self.allowed_tools = allowed_tools or [f"mcp__{MCP_SERVER_NAME}__ask_human"]
        self.telemetry_url = telemetry_url
        self.agent_name = agent_name
        self.memory = memory
        self.memory_auto_inject_limit = memory_auto_inject_limit
        self.model = model
        self.effort = effort
        self.db_pool = db_pool
        # D-71: the dispatcher is injected via a setter after construction (order in
        # main.py: runner before dispatcher). Used in `_run_claude_once`
        # to register the active proc and allow cancel via SIGTERM. Optional
        # — None keeps the pre-D-71 behavior.
        self._dispatcher: Any = None
        # Monotonic counter per (stream, topic) for the live_events seq_num
        # (migration 018). claude_runner emits thinking/tool_use via
        # asyncio.create_task (fire-and-forget); without a seq generated here, the
        # INSERT order in the DB does not follow the SDK stream order
        # and the frontend breaks ties wrongly. No lock: asyncio is single-threaded,
        # the increment is atomic as long as it happens before the first
        # await inside _emit_live.
        self._live_seq_by_topic: dict[str, int] = {}

    def bind_dispatcher(self, dispatcher: Any) -> None:
        """Inject the Dispatcher reference after construction. D-71.

        Used by the runner to call `handler_register_proc` /
        `handler_unregister_proc` around `create_subprocess_exec`.
        """
        self._dispatcher = dispatcher

    async def _team_block(self, ctx: dict[str, Any] | None = None) -> str:
        """Build the filtered '## Team' block: lists the other agents that
        this agent CAN call via ask_agent. Empty if db_pool is missing,
        the agent has no eligible peers, or it is in a child conv (a child
        cannot call anyone — listing peers would be misinformation)."""
        if ctx and ctx.get("is_child"):
            return ""
        if self.db_pool is None or not self.agent_name:
            return ""
        try:
            async with self.db_pool.acquire() as conn:
                policy = await conn.fetchrow(
                    "SELECT can_ask FROM messaging.agent_policies WHERE agent = $1",
                    self.agent_name,
                )
                rows = await conn.fetch(
                    """SELECT u.agent_name AS name, u.full_name AS display_name
                         FROM messaging.users u
                        WHERE u.kind = 'bot'
                          AND u.agent_name IS NOT NULL
                          AND u.agent_name <> $1
                          AND u.is_active = true
                        ORDER BY u.agent_name""",
                    self.agent_name,
                )
        except Exception:
            log.exception("runner.team_block_failed", agent=self.agent_name)
            return ""
        whitelist = policy["can_ask"] if policy and policy["can_ask"] is not None else None
        peers = []
        for r in rows:
            name = r["name"]
            if whitelist is not None and name not in whitelist:
                continue
            peers.append(f"- **{name}** ({r['display_name']})")
        if not peers:
            return (
                "\n\n## Team\n\n"
                "_No other agents available for `ask_agent` right now. "
                "Use `ask_human` if you need to delegate._"
            )
        return (
            "\n\n## Team (agents you can call via `ask_agent` or `ask_agents_many`)\n\n"
            + "\n".join(peers)
        )

    async def _resolve_invocation_context(
        self, conv_id: int | None
    ) -> dict[str, Any]:
        """Detect whether the current conv is a child of another one (and whose). Source of
        truth: `messaging.conversations.parent_conv_id` — the same one used by the
        MCP gate (server.py) that rejects ask_human/ask_agent in a child with 409.

        Returns `{"is_child": bool, "parent_label": str | None}`. Without
        db_pool / conv_id / row — returns `is_child=False` (default root),
        which is the most permissive behavior and matches runs without a broker
        (tests / mock).
        """
        default: dict[str, Any] = {"is_child": False, "parent_label": None}
        if self.db_pool is None or conv_id is None:
            return default
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT pc.id AS parent_id,
                              ps.name AS parent_stream,
                              pu.agent_name AS parent_agent_name
                         FROM messaging.conversations c
                         LEFT JOIN messaging.conversations pc ON pc.id = c.parent_conv_id
                         LEFT JOIN messaging.streams ps ON ps.id = pc.stream_id
                         LEFT JOIN messaging.users pu
                                ON pu.agent_name = ps.name AND pu.kind = 'bot'
                        WHERE c.id = $1""",
                    conv_id,
                )
        except Exception:
            log.exception("runner.invocation_context_failed", agent=self.agent_name)
            return default
        if row is None or row["parent_id"] is None:
            return default
        return {
            "is_child": True,
            "parent_label": row["parent_agent_name"] or row["parent_stream"],
        }

    @staticmethod
    def _invocation_context_block(ctx: dict[str, Any]) -> str:
        """The '## Invocation mode' block — injected **only in a child**. In a root,
        the "## Team" block below already implicitly signals that the agent
        can call peers; no redundant "you are root" block is needed.

        In a child, a short block: parent name + the reply-as-gateway rule.
        The auto reply always works; messaging tools in a child fail with 409.
        """
        if not ctx.get("is_child"):
            return ""
        parent_label = ctx.get("parent_label") or "unknown agent"
        return (
            "\n\n## Invocation mode\n\n"
            f"This conv is a **child** of **`{parent_label}`** (`ask_agent` or "
            "phase handoff). `ask_human`/`ask_agent`/`ask_agents_many` "
            "are blocked here (MCP gate 409, root->child 1-level hierarchy).\n\n"
            f"To request external input (human decision, specialist, "
            f"opinion outside scope): **describe the request at the end of "
            f"the response in a format ready for `{parent_label}` to forward "
            f"verbatim** and end the turn. The parent receives the auto-reply "
            "and routes it.\n\n"
            "Any instruction like \"call `ask_human` X\" / \"call "
            "`ask_agent <Y>` X\" must be read as \"describe in the reply: "
            "needs X\".\n"
        )

    async def _task_state_block(self, topic_key: TopicKey) -> str:
        """The '## Task state' block — when the topic is `task-<slug>`, lists the
        canonical state (workflow, current_step, complexity, baseline,
        worktrees, phases_done) already resolved by the framework. Avoids the
        `get_task_state` ritual at the start of each phase.

        Not injected if the topic is not task-* (analysis mode / free chat).
        """
        if self.db_pool is None:
            return ""
        topic = topic_key.topic
        if not topic.startswith("task-"):
            return ""
        slug = topic[5:]
        if not slug:
            return ""
        try:
            async with self.db_pool.acquire() as conn:
                task = await conn.fetchrow(
                    """SELECT id, slug, title, workflow, status, current_step,
                              current_agent, complexity, impact, difficulty,
                              origin, origin_stream, origin_topic, blocked_reason,
                              metadata_extra
                         FROM tasks.tasks
                        WHERE slug = $1 AND archived_at IS NULL""",
                    slug,
                )
                if task is None:
                    return ""
                phases = await conn.fetch(
                    """SELECT step, agent, artifact, completed_at
                         FROM tasks.phases
                        WHERE task_id = $1 AND completed_at IS NOT NULL
                        ORDER BY completed_at""",
                    task["id"],
                )
                worktrees = await conn.fetch(
                    """SELECT repo, path, branch
                         FROM tasks.worktrees
                        WHERE task_id = $1
                        ORDER BY repo""",
                    task["id"],
                )
        except Exception:
            log.exception("runner.task_state_block_failed", agent=self.agent_name, slug=slug)
            return ""
        meta = task["metadata_extra"]
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        elif not isinstance(meta, dict):
            meta = {}
        baseline = (meta or {}).get("baseline") or {}
        lines: list[str] = ["\n\n## Task state\n"]
        lines.append(f"- **Slug:** `{task['slug']}`")
        lines.append(f"- **Title:** {task['title']}")
        if task["workflow"]:
            lines.append(f"- **Workflow:** `{task['workflow']}`")
        lines.append(f"- **Status:** `{task['status']}`")
        if task["current_step"]:
            agent_str = f" (agent: `{task['current_agent']}`)" if task["current_agent"] else ""
            lines.append(f"- **Current step:** `{task['current_step']}`{agent_str}")
        if task["complexity"]:
            lines.append(f"- **Complexity:** `{task['complexity']}`")
        for k, label in (("impact", "Impact"), ("difficulty", "Difficulty")):
            if task[k]:
                lines.append(f"- **{label}:** `{task[k]}`")
        if task["blocked_reason"]:
            lines.append(f"- **Blocked:** {task['blocked_reason']}")
        if phases:
            done_str = " -> ".join(
                f"`{p['step']}`" + (f" ({p['artifact']})" if p["artifact"] else "")
                for p in phases
            )
            lines.append(f"- **Phases done:** {done_str}")
        else:
            lines.append("- **Phases done:** _(none — task just started)_")
        if baseline:
            base_str = ", ".join(f"`{repo}@{sha[:8]}`" for repo, sha in sorted(baseline.items()))
            lines.append(f"- **Registered baselines:** {base_str}")
        else:
            lines.append("- **Registered baselines:** _(none)_")
        if worktrees:
            wt_lines = [
                f"  - `{w['repo']}` at `{w['path']}` (branch `{w['branch']}`)"
                for w in worktrees
            ]
            lines.append("- **Active worktrees:**")
            lines.extend(wt_lines)
        else:
            lines.append("- **Active worktrees:** _(none)_")
        if task["origin_stream"] and task["origin_topic"]:
            lines.append(
                f"- **Origin:** stream `{task['origin_stream']}`, topic "
                f"`{task['origin_topic']}` (terminal notification comes back here)"
            )
        lines.append("")
        lines.append(
            "_State read from the DB at spawn time (snapshot). For fresh data "
            "after a mid-turn transition, call `get_task_state` via MCP._"
        )
        return "\n".join(lines)

    async def _load_current_step_doc(
        self, topic_key: TopicKey
    ) -> tuple[str, str, dict] | None:
        """Resolve `(workflow_name, step_name, step_dict)` for the task in
        progress in the topic.

        Returns None when: db_pool is missing, the topic is not a task, the slug is empty,
        the task is archived/nonexistent, the workflow has no current step, workflows.yaml
        is missing/unparseable, or the step is not declared in the yaml.

        Shared between `_step_instructions_block` (block in the system prompt)
        and `_step_overrides` (runtime config). Each call reads the yaml from disk —
        edits via the PWA apply on the next spawn without a rebuild.
        """
        if self.db_pool is None:
            return None
        topic = topic_key.topic
        if not topic.startswith("task-"):
            return None
        slug = topic[5:]
        if not slug:
            return None
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT workflow, current_step
                         FROM tasks.tasks
                        WHERE slug = $1 AND archived_at IS NULL""",
                    slug,
                )
        except Exception:
            log.exception("runner.current_step_query_failed", slug=slug)
            return None
        if row is None:
            return None
        wf_name = row["workflow"]
        step_name = row["current_step"]
        if not wf_name or not step_name:
            return None
        try:
            wf_text = _WORKFLOWS_YAML_PATH.read_text(encoding="utf-8")
            wf_doc = yaml.safe_load(wf_text) or {}
        except FileNotFoundError:
            return None
        except Exception:
            log.exception("runner.workflows_yaml_parse_failed")
            return None
        wf = (wf_doc.get("workflows") or {}).get(wf_name) or {}
        step = (wf.get("steps") or {}).get(step_name) or {}
        if not isinstance(step, dict):
            return None
        return wf_name, step_name, step

    async def _step_instructions_block(self, topic_key: TopicKey) -> str:
        """The '## Current phase instructions' block — reads `workflows.yaml` and injects
        the `instructions` (markdown) declared by the task's current step.

        With no task in progress, no step, or no declared `instructions`,
        returns "" (silence — agents without an active step are analysis/free chat).
        """
        doc = await self._load_current_step_doc(topic_key)
        if doc is None:
            return ""
        wf_name, step_name, step = doc
        instructions = step.get("instructions")
        if not isinstance(instructions, str) or not instructions.strip():
            return ""
        artifact = step.get("artifact")
        next_steps = step.get("next") or []
        header_lines = [
            "\n\n## Current phase instructions",
            "",
            f"_Workflow `{wf_name}` -> step `{step_name}`._",
        ]
        if artifact:
            header_lines.append(f"_Expected artifact: `{artifact}`._")
        if next_steps:
            nxt = " | ".join(f"`{n}`" for n in next_steps)
            header_lines.append(f"_Valid transitions: {nxt}._")
        header_lines.append("")
        return "\n".join(header_lines) + instructions.rstrip() + "\n"

    async def _step_overrides(self, topic_key: TopicKey) -> dict:
        """Return the `overrides` sub-dict declared on the task's current step
        in `workflows.yaml`. Empty if there is no task / step / overrides.

        Keeps only whitelisted keys (`model`, `effort`, `memory`) and
        validates basic types. Invalid values in the YAML (manual edits outside
        the PWA) are ignored with a warn log — never crashes the spawn.
        """
        doc = await self._load_current_step_doc(topic_key)
        if doc is None:
            return {}
        _, step_name, step = doc
        raw = step.get("overrides")
        if not isinstance(raw, dict):
            return {}
        out: dict = {}
        model = raw.get("model")
        if isinstance(model, str) and model.strip():
            out["model"] = model.strip()
        elif "model" in raw:
            log.warning(
                "runner.step_overrides_invalid",
                step=step_name, field="model", value=model,
            )
        effort = raw.get("effort")
        if isinstance(effort, str) and effort.strip():
            out["effort"] = effort.strip()
        elif "effort" in raw:
            log.warning(
                "runner.step_overrides_invalid",
                step=step_name, field="effort", value=effort,
            )
        mem = raw.get("memory")
        if isinstance(mem, dict):
            mem_out: dict = {}
            if "enabled" in mem:
                if isinstance(mem["enabled"], bool):
                    mem_out["enabled"] = mem["enabled"]
                else:
                    log.warning(
                        "runner.step_overrides_invalid",
                        step=step_name, field="memory.enabled", value=mem["enabled"],
                    )
            if "auto_inject_limit" in mem:
                lim = mem["auto_inject_limit"]
                if isinstance(lim, int) and not isinstance(lim, bool) and lim >= 0:
                    mem_out["auto_inject_limit"] = lim
                else:
                    log.warning(
                        "runner.step_overrides_invalid",
                        step=step_name, field="memory.auto_inject_limit", value=lim,
                    )
            if mem_out:
                out["memory"] = mem_out
        elif "memory" in raw:
            log.warning(
                "runner.step_overrides_invalid",
                step=step_name, field="memory", value=mem,
            )
        return out

    async def _build_system_prompt(
        self,
        conv_id: int | None = None,
        topic_key: TopicKey | None = None,
    ) -> str:
        """Concatenate the sections enabled in system_prompts/config.yaml.

        Static sections (toggleable):
          - platform.md (framework invariant rules)
          - the agent's CLAUDE.md (role identity)
          - company/CONTEXT.md (instance catalog)
          - company/philosophy.md (optional)

        Dynamic sections (toggleable, generated on every spawn from the DB / FS):
          - "## Invocation mode" — root vs child + parent name (if child)
          - "## Task state" — when topic = task-*, a snapshot of the state
          - "## Current phase instructions" — when the task's step has `instructions`
            declared in workflows.yaml
          - "## Team" — peers the agent can call via ask_agent

        D-63: nothing hardcoded — everything is read from the FS / DB on every invocation. Edits
        via the PWA (or directly in the files) apply to the next task, without a restart.
        D-61: the agent's CLAUDE.md goes in here (no longer via the cwd walker), making
        identity cwd-independent.
        """
        toggles = _load_system_prompt_toggles()
        parts: list[str] = []

        # Resolve root vs child once — used both by the invocation block
        # and by team_block (which omits peers in a child to avoid
        # misinformation: a child cannot call anyone).
        invocation_ctx = await self._resolve_invocation_context(conv_id)

        if toggles["include_platform_prompt"]:
            try:
                platform_text = _PLATFORM_PROMPT_PATH.read_text(encoding="utf-8")
            except FileNotFoundError:
                raise RuntimeError(
                    f"system_prompt: {_PLATFORM_PROMPT_PATH} missing from the "
                    "agent image. Symptom of an incomplete build — rebuild "
                    "the `agent` image (the COPY of framework/system_prompts "
                    "should have brought the file in). Toggle it off in "
                    "system_prompts/config.yaml only as a last resort."
                )
            parts.append(platform_text)

        if toggles.get("include_invocation_context", True):
            invocation = self._invocation_context_block(invocation_ctx)
            if invocation:
                parts.append(invocation)

        if topic_key is not None:
            if toggles.get("include_task_state", True):
                state = await self._task_state_block(topic_key)
                if state:
                    parts.append(state)
            if toggles.get("include_step_instructions", True):
                step = await self._step_instructions_block(topic_key)
                if step:
                    parts.append(step)

        if toggles["include_agent_claude_md"] and self.agent_name:
            agent_md = Path(f"/app/agents/{self.agent_name}/CLAUDE.md")
            agent_md_text = _read_capped(agent_md)
            if agent_md_text.strip():
                parts.append("\n\n# Agent instructions\n\n" + agent_md_text)

        if toggles["include_company_context"]:
            company_ctx = _read_capped(_COMPANY_CONTEXT_PATH)
            if company_ctx.strip():
                parts.append("\n\n# Company context\n\n" + company_ctx)

        if toggles["include_company_philosophy"]:
            phi = _read_capped(_COMPANY_PHILOSOPHY_PATH, cap=4 * 1024)
            # `_(optional` / `_(opcional` are seed markers in the example file
            # that mean "instance hasn't filled this in yet" — skip injection.
            if phi.strip() and "_(optional" not in phi and "_(opcional" not in phi:
                parts.append("\n\n# Operational philosophy\n\n" + phi)

        if toggles["include_team_block"]:
            team = await self._team_block(invocation_ctx)
            if team:
                parts.append(team)

        return "".join(parts)

    async def _resolve_spawn_cwd(self, default_cwd: Path, topic_key: TopicKey) -> Path:
        """Return the cwd to launch `claude -p` in.

        The worktree is used ONLY when this run is executing a workflow phase of
        task `<slug>`, identified by `topic_key.topic` in the format
        `task-<slug>`. Ad-hoc topics (free chat, `__ask-from-*`, `__child-*`,
        date labels) stay in the session_dir even if the agent has other
        tasks in_progress in parallel (D-97).

        Rule: if `topic = task-<slug>` AND there is a registered worktree for that
        `<slug>` whose `current_agent` is this agent AND `status='in_progress'`
        AND it has a path on disk, use that path. Otherwise, default_cwd.

        Before (D-61), the lookup was only by `current_agent`+`in_progress`, without
        a join on the topic slug. An agent with 1 task in_progress in conv X saw the
        task cwd leak into another concurrent conv Y — `--resume` failed
        with a ghost because Y's sid lived in the old cwd's project dir. Restricting
        it to the task's own topic eliminates the cross-talk.

        Why use the worktree: claude_code natively loads `.claude/{agents,commands}/`
        + the repo's `CLAUDE.md` when cwd=worktree. The agent's identity
        goes through cwd-independent channels (system prompt, `--add-dir`).
        """
        if not self.agent_name or self.db_pool is None:
            return default_cwd
        # Neutral topic: we only recognize the `task-` prefix (generated by the reactor
        # in `target_topic = task-<slug>`). Without it, there is no way to map topic
        # → task slug without heuristics.
        topic = topic_key.topic
        if not topic.startswith("task-"):
            return default_cwd
        task_slug = topic[len("task-"):]
        if not task_slug:
            return default_cwd
        try:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT w.path
                    FROM tasks.worktrees w
                    JOIN tasks.tasks t ON t.id = w.task_id
                    WHERE t.slug = $1
                      AND t.current_agent = $2
                      AND t.status = 'in_progress'
                      AND w.path IS NOT NULL
                    """,
                    task_slug, self.agent_name,
                )
        except Exception as e:
            log.warning(
                "runner.spawn_cwd_lookup_failed",
                err=str(e), agent=self.agent_name, task_slug=task_slug,
            )
            return default_cwd
        if len(rows) != 1:
            # 0: task without a worktree (e.g. before execution) or the agent is not
            #    that task's current_agent. >1: ambiguous (multi-repo) — the agent
            #    navigates via absolute paths with --add-dir.
            return default_cwd
        path = Path(rows[0]["path"])
        if not path.is_dir():
            log.warning(
                "runner.spawn_cwd_missing_path",
                agent=self.agent_name,
                task_slug=task_slug,
                path=str(path),
            )
            return default_cwd
        return path

    async def _inject_memory(self, prompt: str, overrides: dict | None = None) -> str:
        """Prepend the N most relevant memory facts to the prompt.
        No-op if memory is off (on the agent or via the step override) or limit<=0.

        Accepted overrides (from `workflows.yaml.steps.<step>.overrides.memory`):
          - `enabled=False` → disables the inject for this spawn (even if the agent
            has a MemoryStore configured).
          - `auto_inject_limit=N` → uses N instead of the agent's default.
        """
        mem_ov = (overrides or {}).get("memory") or {}
        if mem_ov.get("enabled") is False:
            log.info(
                "runner.memory_override_applied",
                agent=self.agent_name, field="enabled", to=False,
            )
            return prompt
        limit = mem_ov.get("auto_inject_limit", self.memory_auto_inject_limit)
        if mem_ov.get("auto_inject_limit") is not None and limit != self.memory_auto_inject_limit:
            log.info(
                "runner.memory_override_applied",
                agent=self.agent_name, field="auto_inject_limit",
                from_=self.memory_auto_inject_limit, to=limit,
            )
        if self.memory is None or limit <= 0:
            return prompt
        try:
            facts = await self.memory.recall(prompt, limit=limit)
        except Exception:
            log.exception("runner.memory_recall_failed")
            return prompt
        if not facts:
            return prompt
        lines = ["## Memory (facts you've already recorded about this context)", ""]
        for f in facts:
            tag_str = f" [{', '.join(f['tags'])}]" if f.get("tags") else ""
            lines.append(f"- **{f['key']}**{tag_str}: {f['value']}")
        lines.extend([
            "",
            "_(The facts above are historical context only — pulled from your persistent memory. "
            "Use `memory_recall` to search for more, `memory_save` to add new facts relevant to future sessions.)_",
            "",
            "---",
            "",
            prompt,
        ])
        log.info("runner.memory_injected", count=len(facts), agent=self.agent_name)
        return "\n".join(lines)

    async def _emit_telemetry(self, payload: dict) -> None:
        """Fire-and-forget to the web (with a short timeout). Fails silently."""
        if not self.telemetry_url or not self.agent_name:
            return
        payload = {**payload, "agent": self.agent_name}
        try:
            timeout = aiohttp.ClientTimeout(total=3)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(self.telemetry_url, json=payload, headers=_auth_headers()) as r:
                    if r.status >= 400:
                        log.debug("runner.telemetry_bad_status", status=r.status)
        except Exception as e:
            log.debug("runner.telemetry_failed", error=str(e))

    async def _emit_live(self, topic_key: TopicKey, kind: str, summary: str = "", data: dict | None = None) -> None:
        """Fire-and-forget to the live trace endpoint (same base URL as telemetry,
        replacing '/event' -> '/live-event'). Silently skipped on failure.

        `seq_num` (migration 018) is generated here, before the fire-and-forget,
        to guarantee a deterministic order even when the POSTs reach the
        DB out of order. Per-conversation counter (stream+topic), incremented
        under a lock to cover concurrent emits.
        """
        if not self.telemetry_url or not self.agent_name:
            return
        live_url = self.telemetry_url.rsplit("/event", 1)[0] + "/live-event"
        slug = topic_key.slug()
        seq = self._live_seq_by_topic.get(slug, 0) + 1
        self._live_seq_by_topic[slug] = seq
        # High cap (8KB) to fit multi-paragraph thinking; the UI renders it
        # in full. If exceeded, the backend still has a second safe cap.
        payload = {
            "stream": topic_key.stream,
            "topic": topic_key.topic,
            "agent": self.agent_name,
            "kind": kind,
            "summary": summary[:8000] if summary else "",
            "data": data or {},
            "seq_num": seq,
        }
        try:
            timeout = aiohttp.ClientTimeout(total=2)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(live_url, json=payload, headers=_auth_headers()) as r:
                    if r.status >= 400:
                        log.debug("runner.live_bad_status", status=r.status, kind=kind)
        except Exception as e:
            log.debug("runner.live_failed", error=str(e))

    async def _run_claude_once(
        self,
        *,
        prompt: str,
        workdir: Path,
        mcp_config_path: Path,
        resume_sid: str | None,
        resume_sid_cwd: str | None,
        topic_key: TopicKey,
        conv_id: int | None = None,
        overrides: dict | None = None,
    ) -> RunOutcome:
        """One invocation of claude -p. Returns the outcome (posts nothing).

        `resume_sid_cwd` is the cwd where `resume_sid` was stored (D-97). If it differs
        from the `spawn_cwd` resolved now, we proactively skip `--resume` — the CLI
        stores `<sid>.jsonl` in `~/.claude/projects/<encoded-cwd>/`, and resuming
        from another cwd produces a ghost. None means "legacy id without a registered cwd"
        (back-compat with sids stored before migration 024) — accepted.
        """
        import os
        # Build the system prompt ONCE before the mock/real branch — so the
        # assembly telemetry shows up on both paths. Prompt edits
        # (platform.md, workflows.yaml, CLAUDE.md) are read from the FS here.
        sys_prompt = await self._build_system_prompt(conv_id=conv_id, topic_key=topic_key)
        log.info(
            "runner.sys_prompt_assembled",
            agent=self.agent_name,
            topic=topic_key.slug(),
            chars=len(sys_prompt),
            has_invocation_block="\n## Invocation mode\n" in sys_prompt,
            has_task_state_block="\n## Task state\n" in sys_prompt,
            has_step_instructions_block="\n## Current phase instructions\n" in sys_prompt,
            has_team_block="\n## Team" in sys_prompt,
        )
        # Mock mode for e2e tests: does not invoke Claude, returns a scripted answer.
        # Accepts "0"/"false"/"no"/"off"/empty as off (otherwise `CLAUDE_MOCK=0`
        # would enable mock because `"0"` is truthy in Python).
        _mock_flag = os.environ.get("CLAUDE_MOCK", "").strip().lower()
        if _mock_flag not in ("", "0", "false", "no", "off"):
            mock_reply = os.environ.get("CLAUDE_MOCK_REPLY",
                f"[mock reply] agent {self.agent_name} received prompt ({len(prompt)} chars)")
            log.info("runner.mock_reply", topic=topic_key.slug(), agent=self.agent_name)
            # Emit synthetic live events so the PWA shows activity in mock mode too.
            await self._emit_live(
                topic_key, "run_start",
                summary=f"mock resume={bool(resume_sid)}",
            )
            await self._emit_live(
                topic_key, "run_end",
                summary="mock turns=1 cost=$0.0000 dur=1ms",
                data={"mock": True},
            )
            return RunOutcome(
                ok=True, rc=0, result_text=mock_reply, session_id=resume_sid,
                total_cost_usd=0.0, duration_ms=1, num_turns=1,
                usage={"input_tokens": len(prompt), "output_tokens": len(mock_reply)},
                tool_uses=[],
            )
        # D-90: synchronous refresh of the host's OAuth credentials before each
        # spawn. /tmp/host-claude/ is a dir bind of the host's ~/.claude/ (inode-safe
        # via path resolution); we copy it into the agent's volume so it can keep
        # writing refresh tokens rw locally. Cost: 1 cp of ~500 bytes.
        # Without this, the container is stuck with the token captured in the entrypoint, and
        # when the host rotates it (write-then-rename = new inode) the old file bind
        # pointed to the dead inode -> silent 401 (D-55).
        try:
            host_creds = Path("/tmp/host-claude/.credentials.json")
            if host_creds.is_file():
                shutil.copyfile(host_creds, "/home/node/.claude/.credentials.json")
        except Exception as e:
            log.debug("runner.creds_sync_failed", error=str(e))

        cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose"]
        # sys_prompt was built and logged at the start of the function (before the
        # mock/real branch) — passed on via --append-system-prompt.
        cmd.extend(["--append-system-prompt", sys_prompt])
        ov = overrides or {}
        final_model = ov.get("model") or self.model
        final_effort = ov.get("effort") or self.effort
        if ov.get("model") and ov["model"] != self.model:
            log.info(
                "runner.step_overrides_applied",
                agent=self.agent_name, field="model",
                from_=self.model, to=ov["model"],
            )
        if ov.get("effort") and ov["effort"] != self.effort:
            log.info(
                "runner.step_overrides_applied",
                agent=self.agent_name, field="effort",
                from_=self.effort, to=ov["effort"],
            )
        if final_model:
            cmd.extend(["--model", final_model])
        if final_effort:
            cmd.extend(["--effort", final_effort])
        if mcp_config_path.exists():
            cmd.extend(["--mcp-config", str(mcp_config_path), "--strict-mcp-config"])
            if self.allowed_tools:
                cmd.extend(["--allowed-tools", " ".join(self.allowed_tools)])
        # Allow reads outside the cwd (the sandbox blocks them without --add-dir).
        # The agent's knowledge is included so it stays reachable even with cwd=worktree.
        # Subdirs of /workspace/company (tasks, ideas, notes, decisions) are included
        # explicitly: Claude Code allows Write in subdirs of --add-dir, but
        # the Bash tool restricts mkdir to --add-dir roots only (D-TODO).
        extra_dirs = [
            "/workspace/company",
            "/workspace/company/tasks",
            "/workspace/company/ideas",
            "/workspace/company/notes",
            "/workspace/company/decisions",
            "/workspace/repos",
        ]
        if self.agent_name:
            extra_dirs.append(f"/app/agents/{self.agent_name}/knowledge")
        for extra in extra_dirs:
            if Path(extra).exists():
                cmd.extend(["--add-dir", extra])

        # spawn_cwd = the task's worktree if the topic is `task-<slug>` AND that task has a
        # linked worktree with this agent in_progress; otherwise workdir (D-97).
        # Lets Claude Code natively load the repo's `.claude/{agents,commands}/` and `CLAUDE.md`.
        # The agent's identity goes in `--append-system-prompt`.
        spawn_cwd = await self._resolve_spawn_cwd(workdir, topic_key)

        # `--resume` only works if the spawn_cwd matches the cwd where the
        # `<resume_sid>.jsonl` was written (the Claude CLI stores projects per-cwd).
        # When the resolved cwd differs from the registered one, we skip `--resume` instead
        # of sending it to a ghost — the sid stays in the DB for a possible future run
        # that returns to the same cwd. Without `--resume`, this run loses the CLI conv
        # buffer, but the rest of the context comes from the system prompt + prompt.
        cwd_str = str(spawn_cwd)
        resume_skipped_reason: str | None = None
        if resume_sid and resume_sid_cwd is not None and resume_sid_cwd != cwd_str:
            resume_skipped_reason = "cwd_mismatch"
            log.warning(
                "runner.resume_skipped",
                topic=topic_key.slug(),
                resume_sid=resume_sid,
                saved_cwd=resume_sid_cwd,
                spawn_cwd=cwd_str,
                reason=resume_skipped_reason,
                hint="sid registered in another cwd; fresh spawn, sid kept in the DB",
            )
            resume_sid = None
        if resume_sid:
            cmd.extend(["--resume", resume_sid])

        log.info(
            "runner.spawn",
            topic=topic_key.slug(),
            cwd=cwd_str,
            workdir=str(workdir),
            resume=bool(resume_sid),
            resume_skipped=resume_skipped_reason,
            prompt_len=len(prompt),
            mcp=mcp_config_path.exists(),
        )

        # The Claude Code CLI creates auto-memory in ~/.claude/projects/<slug>/memory/
        # per session. We disable it — we use memory_save/recall (Postgres) via MCP.
        #
        # Claude CLI MCP env vars (two distinct timeouts — easy to mix up):
        #   MCP_TIMEOUT        — MCP server startup (default 30s). We keep the
        #                        default; our MCP servers start in ms.
        #   MCP_TOOL_TIMEOUT   — per-tool-call timeout. The ~60s default broke
        #                        ask_human (blocks waiting for the human, can
        #                        take hours) and ask_agent (target thinking or
        #                        itself blocked on ask_human). We raise it
        #                        to 24h to cover a full human cycle
        #                        (sleep/meeting/travel). Override via
        #                        MCP_TOOL_TIMEOUT_MS in the env.
        spawn_env = {
            **os.environ,
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "MCP_TOOL_TIMEOUT": str(int(os.environ.get("MCP_TOOL_TIMEOUT_MS", 24 * 60 * 60 * 1000))),
        }
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(spawn_cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=spawn_env,
            limit=STREAM_STDOUT_LIMIT_BYTES,
        )
        # D-71: register the proc with the dispatcher to allow cancel via SIGTERM.
        # Unregister happens after the stream-consume block below.
        if self._dispatcher is not None:
            try:
                self._dispatcher.handler_register_proc(topic_key, proc)
            except Exception:
                log.exception("runner.register_proc_failed", topic=topic_key.slug())

        result_text = ""
        session_id = resume_sid
        error_subtype: str | None = None
        total_cost_usd: float | None = None
        total_duration_ms: int | None = None
        num_turns: int | None = None
        usage: dict[str, Any] | None = None
        tool_uses: list[str] = []
        # Buffer for the assistant's last text block not yet emitted
        # as "thinking". The intuition: text not followed by a tool_use is the
        # final answer; emitting it as thinking would duplicate the answer bubble.
        # Rule: on a tool_use OR a new text block, flush the previous
        # buffer as thinking. If the stream ends in `result` with no further
        # tool_use, the pending buffer is dropped (no duplicate).
        pending_text_block: str | None = None

        def _flush_pending_thinking():
            nonlocal pending_text_block
            if pending_text_block:
                pt = pending_text_block
                pending_text_block = None
                asyncio.create_task(self._emit_live(
                    topic_key, "thinking", summary=pt,
                ))

        assert proc.stdout is not None
        oversized_lines_skipped = 0
        while True:
            # Manual loop (instead of `async for`) to catch
            # LimitOverrunError/ValueError from a huge line and keep
            # reading the next ones. readline() already drains the internal buffer up to the
            # separator when it overflows, so the next iteration resumes at the
            # following line. The turn only fails if the final `result` does not arrive.
            try:
                line_bytes = await proc.stdout.readline()
            except (asyncio.LimitOverrunError, ValueError) as e:
                oversized_lines_skipped += 1
                log.warning(
                    "runner.oversized_line_skipped",
                    topic=topic_key.slug(),
                    limit_bytes=STREAM_STDOUT_LIMIT_BYTES,
                    skipped_so_far=oversized_lines_skipped,
                    err=str(e),
                )
                asyncio.create_task(self._emit_live(
                    topic_key, "tool_result",
                    summary=(
                        f"(output truncated: a stream-json line > "
                        f"{STREAM_STDOUT_LIMIT_BYTES // (1024*1024)} MiB "
                        "was dropped by the framework)"
                    ),
                    data={"is_error": True, "oversized": True},
                ))
                continue
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                log.debug("runner.non_json_line", line=line[:200])
                continue

            t = obj.get("type")
            if t == "system" and obj.get("subtype") == "init":
                session_id = obj.get("session_id") or session_id
                asyncio.create_task(self._emit_live(
                    topic_key, "run_start",
                    summary=f"resume={bool(resume_sid)} model={self.model or '(default)'}",
                ))
            elif t == "assistant":
                msg = obj.get("message") or {}
                for block in msg.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        # The previous pending text is "real thinking" — flush.
                        _flush_pending_thinking()
                        name = str(block.get("name", "?"))
                        tool_uses.append(name)
                        inp = block.get("input") or {}
                        first_kv = next(iter(inp.items()), None)
                        snippet = ""
                        if first_kv:
                            v = str(first_kv[1])
                            if len(v) > 80:
                                v = v[:77] + "..."
                            snippet = f" {first_kv[0]}={v}"
                        # full input in `data.input` — the frontend uses it for
                        # click-to-expand in LiveEventLine. Defensive cap so the
                        # SSE payload doesn't blow up.
                        try:
                            input_json = json.dumps(inp, ensure_ascii=False, default=str)
                        except Exception:
                            input_json = str(inp)
                        if len(input_json) > 20000:
                            input_json = input_json[:19997] + "..."
                        asyncio.create_task(self._emit_live(
                            topic_key, "tool_use",
                            summary=f"{name}{snippet}",
                            data={"tool": name, "input": input_json},
                        ))
                    elif block.get("type") == "text":
                        text = (block.get("text") or "").strip()
                        if text:
                            # If there was previous pending text, it is confirmed
                            # thinking (another text follows in the flow). Flush.
                            _flush_pending_thinking()
                            # This new text is buffered — it only becomes "thinking"
                            # if a tool_use comes after it. If `result` comes
                            # first, it is the final answer and we don't emit it (dedup
                            # against the bot message bubble).
                            pending_text_block = text
            elif t == "user":
                # tool_result blocks come as a user msg after claude runs the tool
                msg = obj.get("message") or {}
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        is_err = block.get("is_error", False)
                        # Always extract the text (success + error) so the frontend
                        # can show the output in the expanded OUT. content can be a
                        # string or list[{type:text, text:...}].
                        content = block.get("content")
                        out_text = ""
                        if isinstance(content, str):
                            out_text = content
                        elif isinstance(content, list):
                            parts = []
                            for c in content:
                                if isinstance(c, dict):
                                    parts.append(c.get("text") or c.get("content") or "")
                                else:
                                    parts.append(str(c))
                            out_text = "\n".join(p for p in parts if p)
                        out_text = out_text.strip()
                        if is_err:
                            summary = out_text or "error"
                        else:
                            summary = "ok"
                        # Inline cap at ~5KB to fit in pg_notify (the 8KB limit
                        # includes the JSON wrapper + id/agent/ts/kind/summary).
                        # The full version goes in `output_full` (only used if
                        # truncated); a trigger strips that field before
                        # emitting the NOTIFY so it doesn't overflow. The frontend fetches
                        # the full one via GET /api/live_events/{id}/full.
                        OUTPUT_INLINE_CAP = 5000
                        OUTPUT_FULL_CAP = 200_000  # protects the DB/transport
                        full_text = out_text[:OUTPUT_FULL_CAP]
                        is_truncated = len(out_text) > OUTPUT_INLINE_CAP
                        data_payload: dict = {
                            "is_error": is_err,
                            "output": out_text[:OUTPUT_INLINE_CAP],
                        }
                        if is_truncated:
                            data_payload["output_truncated"] = True
                            data_payload["output_full_len"] = len(out_text)
                            # Archive the full text in the DB (the trigger strips it before the NOTIFY).
                            data_payload["output_full"] = full_text
                        asyncio.create_task(self._emit_live(
                            topic_key, "tool_result",
                            summary=summary,
                            data=data_payload,
                        ))
            elif t == "result":
                # Do NOT flush pending_text_block here — if it was not
                # interrupted by a tool_use, it is the final answer and will
                # arrive as result_text below. Emitting it as thinking
                # would duplicate the answer bubble (F3 dedup).
                pending_text_block = None
                result_text = obj.get("result", "") or ""
                session_id = obj.get("session_id") or session_id
                if obj.get("subtype") != "success":
                    error_subtype = obj.get("subtype")
                total_cost_usd = obj.get("total_cost_usd")
                total_duration_ms = obj.get("duration_ms") or obj.get("total_duration_ms")
                num_turns = obj.get("num_turns")
                usage = obj.get("usage")
                cost_str = f"${total_cost_usd:.4f}" if total_cost_usd is not None else "?"
                asyncio.create_task(self._emit_live(
                    topic_key, "run_end",
                    summary=f"turns={num_turns or '?'} cost={cost_str} dur={total_duration_ms or '?'}ms",
                    # Structured fields for the UI to sum in the topic header.
                    data={
                        "tool_uses": tool_uses,
                        "subtype": obj.get("subtype"),
                        "cost_usd": total_cost_usd,
                        "duration_ms": total_duration_ms,
                        "num_turns": num_turns,
                    },
                ))

        rc = await proc.wait()
        stderr_bytes = await proc.stderr.read() if proc.stderr else b""
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")

        # D-71: the proc exited — release the ref so the dispatcher doesn't try to SIGTERM
        # a dead proc. Idempotent; ProcessLookupError is already tolerated on the
        # cancel path too.
        if self._dispatcher is not None:
            try:
                self._dispatcher.handler_unregister_proc(topic_key)
            except Exception:
                log.debug("runner.unregister_proc_failed", topic=topic_key.slug())

        # D-71: make sure the PWA badge transitions out of "running".
        # The normal `run_end` live_event is emitted when the CLI emits
        # `type=result` in the stream-json. On a hard kill (SIGKILL) or crash, the
        # CLI exits without emitting result — the frontend kept seeing
        # `runner_state=running` indefinitely. We detect it via
        # `total_duration_ms is None` (only set on the result path) and
        # emit a synthetic run_end with subtype=error.
        if total_duration_ms is None and rc != 0:
            synthetic_subtype = "killed" if rc in (-15, 143, -9, 137) else "error"
            stderr_preview = stderr_text.strip().splitlines()[-1] if stderr_text.strip() else ""
            asyncio.create_task(self._emit_live(
                topic_key, "run_end",
                summary=f"exit={rc} subtype={synthetic_subtype}"
                        + (f" · {stderr_preview[:160]}" if stderr_preview else ""),
                data={
                    "subtype": synthetic_subtype,
                    "synthetic": True,
                    "rc": rc,
                    "tool_uses": tool_uses,
                },
            ))

        # Spurious exit code: the stream-json returned `result subtype=success`
        # (i.e. work complete + duration_ms set + no error_subtype),
        # but the process died with rc != 0. CLI bug on shutdown (cleanup of MCP /
        # fd / async task). Reporting "❌ claude error" in this case confuses the
        # human and discards a turn that actually succeeded. We treat it as ok and
        # only log it, to keep a signal without masking it in case it becomes widespread.
        spurious_exit = (
            rc != 0 and total_duration_ms is not None and not error_subtype
        )
        if spurious_exit:
            log.warning(
                "runner.spurious_exit_code",
                topic=topic_key.slug(),
                rc=rc,
                num_turns=num_turns,
                duration_ms=total_duration_ms,
                hint="result success in the stream but the process exited rc!=0",
            )

        ghost_session = bool(GHOST_SESSION_RE.search(stderr_text))

        # Defensive session_id persistence (D-70): the CLI emits a new id
        # in the `system/init` of every invocation (fork on resume), but only writes the
        # matching .jsonl as the run progresses. If the run dies right after
        # init, the id exists at runtime but never touches disk — saving it in the
        # DB creates a ghost that breaks the next attempt. We skip the save in that
        # case and keep the previous id (which is known to exist).
        if session_id and session_id != resume_sid and not _session_jsonl_exists(session_id):
            log.warning(
                "runner.session_id_ghost",
                topic=topic_key.slug(),
                session_id=session_id,
                previous=resume_sid,
                rc=rc,
            )
        elif session_id:
            # D-97: persist the cwd too so future runs can detect a mismatch
            # before trying `--resume`.
            await self.session_mgr.save_session_id(topic_key, session_id, cwd=cwd_str)

        return RunOutcome(
            ok=(
                (rc == 0 or spurious_exit)
                and not error_subtype
                and not (oversized_lines_skipped and not result_text)
            ),
            rc=rc,
            result_text=result_text,
            session_id=session_id,
            error_subtype=error_subtype,
            stderr=stderr_text,
            total_cost_usd=total_cost_usd,
            duration_ms=total_duration_ms,
            num_turns=num_turns,
            usage=usage,
            tool_uses=tool_uses,
            oversized_lines_skipped=oversized_lines_skipped,
            ghost_session=ghost_session,
        )

    async def handle(self, event: dict[str, Any], topic_key: TopicKey, workdir: Path) -> None:
        raw = str(event.get("content") or "")
        prompt = MENTION_RE.sub("", raw).strip()
        if not prompt:
            log.info("runner.empty_prompt", topic=topic_key.slug())
            return

        # Stale handoff filter: handoffs posted by the reactor during a run
        # that already absorbed the phase via --resume continuity sit in the
        # dispatcher queue (LISTEN fires mid-run, the dispatcher enqueues). When the
        # main run ends, the dispatcher consumes that queue — each handoff
        # becomes a new spawn whose only output is "late, already done".
        # Symptom: 2026-05-05 fix-task-16 (2 cascade runs after `done`).
        # Drop when: (1) the task is in a terminal status, or (2) the most
        # recent phase of this step is already completed (`completed_at IS NOT NULL`).
        # We look at the most recent `phases` row by idx — preserves reopen_task,
        # which inserts a new in-flight phase for a step that was completed before
        # (the filter must not drop in that case). The cursor advances normally
        # via mark_processed after handle.
        if self.db_pool is not None:
            ho = HANDOFF_BODY_RE.search(prompt)
            if ho:
                ho_slug = ho.group(1)
                ho_step = ho.group(2)
                try:
                    async with self.db_pool.acquire() as conn:
                        row = await conn.fetchrow(
                            """SELECT t.status,
                                      (
                                        SELECT p.completed_at IS NOT NULL
                                          FROM tasks.phases p
                                         WHERE p.task_id = t.id AND p.step = $2
                                         ORDER BY p.idx DESC LIMIT 1
                                      ) AS latest_phase_done
                                 FROM tasks.tasks t WHERE t.slug = $1""",
                            ho_slug, ho_step,
                        )
                except Exception as e:
                    log.warning(
                        "runner.stale_handoff_check_failed",
                        topic=topic_key.slug(), error=str(e)[:200],
                    )
                    row = None
                if row is not None:
                    is_terminal = row["status"] in ("done", "blocked", "human_review")
                    latest_done = bool(row["latest_phase_done"])
                    if is_terminal or latest_done:
                        log.info(
                            "runner.stale_handoff_dropped",
                            topic=topic_key.slug(),
                            task_slug=ho_slug,
                            handoff_step=ho_step,
                            task_status=row["status"],
                            latest_phase_done=latest_done,
                        )
                        return

        started_at = int(time.time())
        # Resolve step overrides once per handle. Read from `workflows.yaml`
        # via `_step_overrides`. Empty if the topic is not a task / the step has no overrides.
        # Passed to `_inject_memory` and `_run_claude_once` on all
        # attempts of this handle (config does not change mid-handle).
        step_overrides = await self._step_overrides(topic_key)
        # Auto-inject relevant memory (if configured, with overrides).
        prompt = await self._inject_memory(prompt, overrides=step_overrides)

        # The event's conv id — used by the broker (D-87) and by the
        # contextual system prompt blocks (invocation mode, task state).
        conv_id_raw = event.get("conversation_id") if isinstance(event, dict) else None
        try:
            conv_id_for_run = int(conv_id_raw) if conv_id_raw is not None else None
        except (TypeError, ValueError):
            conv_id_for_run = None

        if self.broker is not None:
            # D-87: pass conv_id to the internal broker. Used later by the MCP
            # `__ask_agent` handler to pass parent_conv_id when creating
            # the `__ask-from-*` child conv — hierarchy persisted via the schema,
            # no heuristics.
            self.broker.register_topic(topic_key, conv_id=conv_id_for_run)

        # (There used to be a "thinking..." ack here — removed. The PWA now shows
        # tool_use/thinking live via live_events, which made the ack noise.)

        # MCP config: in-process server + merge of instance/agents/<name>/mcp.extra.json
        # (side capabilities such as playwright-mcp) if it exists.
        mcp_config_path = workdir / ".mcp-config.json"
        if self.broker is not None and self.mcp_url_for is not None:
            url = self.mcp_url_for(topic_key.slug())
            servers: dict = {MCP_SERVER_NAME: {"type": "http", "url": url}}
            if self.agent_name:
                extra_path = Path(f"/app/agents/{self.agent_name}/mcp.extra.json")
                if extra_path.exists():
                    try:
                        extra = json.loads(extra_path.read_text())
                        extra_servers = extra.get("mcpServers") or {}
                        if isinstance(extra_servers, dict):
                            servers.update(extra_servers)
                    except Exception:
                        log.warning(
                            "runner.mcp_extra_parse_failed",
                            path=str(extra_path),
                        )
            mcp_config_path.write_text(
                json.dumps({"mcpServers": servers}, indent=2)
            )
            log.debug(
                "runner.mcp_config",
                topic=topic_key.slug(),
                url=url,
                extra_servers=sorted(s for s in servers if s != MCP_SERVER_NAME),
            )

        # Attempt loop — re-resolves resume_sid between attempts
        # (the first run may save session_id before being killed).
        outcome: RunOutcome | None = None
        attempts_done = 0
        # Ghost session recovery (D-70): if the CLI complains "No conversation
        # found with session ID" (resume pointing to an id that does not exist on
        # disk), we clear the DB and redo the SAME attempt without `--resume`,
        # without consuming a normal attempt. A single recovery per handle —
        # if it fails again, it falls through to the normal retry.
        ghost_recovered = False
        attempt = 0
        while attempt < MAX_ATTEMPTS:
            attempt += 1
            attempts_done = attempt
            session_ref = await self.session_mgr.session_ref_for(topic_key)
            resume_sid = session_ref[0] if session_ref else None
            resume_sid_cwd = session_ref[1] if session_ref else None
            # Retry after oversized: swap the prompt for the recovery one (asks for partial
            # reads), which only makes sense with --resume (so Claude has the context
            # of what it was doing). If there is no session_id yet, we fall back to the
            # original prompt — better than sending the recovery prompt without context.
            current_prompt = prompt
            if (
                attempt > 1
                and outcome is not None
                and outcome.oversized_lines_skipped
                and resume_sid
            ):
                current_prompt = OVERSIZED_RECOVERY_PROMPT
            outcome = await self._run_claude_once(
                prompt=current_prompt,
                workdir=workdir,
                mcp_config_path=mcp_config_path,
                resume_sid=resume_sid,
                resume_sid_cwd=resume_sid_cwd,
                topic_key=topic_key,
                conv_id=conv_id_for_run,
                overrides=step_overrides,
            )
            if outcome.ok:
                break
            # Ghost session: the CLI pointed to a nonexistent sid. Clear the DB so the
            # next read gets None (→ spawn without `--resume`, fresh start)
            # and repeat this attempt without consuming it. Only once, to avoid a loop.
            if outcome.ghost_session and not ghost_recovered and resume_sid:
                ghost_recovered = True
                await self.session_mgr.clear_session_id(topic_key)
                log.warning(
                    "runner.ghost_session_recover",
                    topic=topic_key.slug(),
                    missing_sid=resume_sid,
                    hint="retry without --resume; conversation context lost, artifacts on disk preserve state",
                )
                attempt -= 1  # don't count this attempt — it will be redone fresh
                continue
            # D-72: if rc=143 came from the human's cancel_topic (SIGTERM via
            # /api/cancel or cascade delete), do NOT retry. The dispatcher
            # marked the topic via `_user_cancelled` on SIGTERM — we consume it
            # here to skip the retry and let the turn die.
            if (
                self._dispatcher is not None
                and outcome.retriable
                and self._dispatcher.consume_user_cancel(topic_key)
            ):
                log.info(
                    "runner.user_cancelled",
                    topic=topic_key.slug(),
                    attempt=attempt,
                    rc=outcome.rc,
                    hint="SIGTERM came from a human cancel; skip retry",
                )
                break
            if not outcome.retriable or attempt == MAX_ATTEMPTS:
                break
            log.warning(
                "runner.retrying",
                topic=topic_key.slug(),
                attempt=attempt,
                rc=outcome.rc,
                error_subtype=outcome.error_subtype,
                oversized_lines_skipped=outcome.oversized_lines_skipped,
                will_resume=bool(outcome.session_id),
            )
            await asyncio.sleep(RETRY_BACKOFF_SEC * attempt)

        assert outcome is not None
        usage = outcome.usage or {}
        # Keys aligned with the backend's /api/telemetry/event (main.py):
        #   payload.get("topic_slug"), payload.get("cost_usd"), etc.
        # Extra metadata (num_turns, tool_uses, ...) goes into the JSON metadata.
        # The Claude CLI's 3 input counters are disjoint (same semantics
        # as the Anthropic API) — we store each in its own column. Migration 010
        # reverted the sum that 004 introduced in `input_tokens`.
        base_input = usage.get("input_tokens") or 0
        cache_creation = usage.get("cache_creation_input_tokens") or 0
        cache_read = usage.get("cache_read_input_tokens") or 0
        # task_slug: if the topic starts with 'task-', extract the task slug. The backend
        # resolves conversation_id via (stream, topic) — same practice as live-event.
        task_slug = topic_key.topic[5:] if topic_key.topic.startswith("task-") else None
        telemetry = {
            "topic_slug": topic_key.slug(),
            "stream": topic_key.stream,
            "topic": topic_key.topic,
            "task_slug": task_slug,
            "event_type": "run_end" if outcome.ok else "run_error",
            "duration_ms": outcome.duration_ms,
            "cost_usd": outcome.total_cost_usd,
            "input_tokens": base_input,
            "output_tokens": usage.get("output_tokens"),
            "cache_creation_tokens": cache_creation,
            "cache_read_tokens": cache_read,
            "model": self.model,
            "metadata": {
                "num_turns": outcome.num_turns,
                "started_at": started_at,
                "tool_uses": outcome.tool_uses,
                "exit_code": outcome.rc or 0,
                "error_subtype": outcome.error_subtype,
                "oversized_lines_skipped": outcome.oversized_lines_skipped,
            },
        }

        if not outcome.ok:
            log.warning(
                "runner.claude_failed",
                topic=topic_key.slug(),
                rc=outcome.rc,
                error_subtype=outcome.error_subtype,
                attempts=attempts_done,
                oversized_lines_skipped=outcome.oversized_lines_skipped,
                stderr=outcome.stderr[:500],
            )
            await self._emit_telemetry(telemetry)
            retried_note = f" (after {attempts_done} attempts)" if attempts_done > 1 else ""
            err_msg = (
                f"❌ claude error{retried_note} "
                f"(exit={outcome.rc}, subtype={outcome.error_subtype})\n"
                f"```\n{outcome.stderr[:800] or '(no stderr)'}\n```"
            )
            await self._reply(topic_key, err_msg)
            return

        final = outcome.result_text or "_(empty response)_"
        log.info(
            "runner.done",
            topic=topic_key.slug(),
            out_len=len(final),
            session_id=outcome.session_id,
            total_cost_usd=outcome.total_cost_usd,
            duration_ms=outcome.duration_ms,
            num_turns=outcome.num_turns,
            attempts=attempts_done,
            oversized_lines_skipped=outcome.oversized_lines_skipped,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_read_tokens=usage.get("cache_read_input_tokens"),
            cache_creation_tokens=usage.get("cache_creation_input_tokens"),
            tool_uses=outcome.tool_uses,
        )
        await self._emit_telemetry(telemetry)
        await self._reply(topic_key, final, tool_uses=outcome.tool_uses)

    async def _reply(
        self,
        key: TopicKey,
        content: str,
        tool_uses: list[str] | None = None,
    ) -> None:
        # Per-turn idempotency key: protects against reentrancy/retry of
        # `_reply` that could insert the same msg twice in the conv (race or
        # partial exception). The schema has UNIQUE(conversation_id, client_id)
        # — the broker returns the existing msg on conflict. Cheap defense.
        import uuid as _uuid
        reply_uid = _uuid.uuid4().hex[:16]
        try:
            await self.broker_client.send_message(
                key.stream, key.topic, content,
                client_id=f"reply-{reply_uid}",
            )
        except Exception:
            log.exception("runner.reply_failed")
            return
        # D-100: if this turn ran in a child conv (parent_conv_id IS NOT NULL)
        # created by a handoff via complete_phase, echo the final reply into the parent
        # conv. Before, the parent only saw the answer if it had delegated via
        # ask_agent — and on that path the asker does subscribe_to_conversation
        # on the `__child-*` conv created by the broker. For a phase handoff
        # (`task-<slug>` in the child's stream), there was no equivalent
        # subscription — the reply was orphaned in the DB. Skip `__*` topics
        # because ask_agent already auto-routes via subscribe_to_conversation
        # and would produce a duplicate.
        if key.topic.startswith("__"):
            return
        # Skip the echo if this turn called `complete_phase`. The reactor will already
        # post a handoff (or terminal) in the parent conv with the complete_phase
        # summary — echoing the child's reply duplicates the delivery to the parent
        # and makes it wake up twice for the same deliverable. Symptom observed on
        # 2026-04-28 (the parent received a "Handoff from <child>" followed by a
        # "Reply from <child>" with the same content, processed 2 turns and answered
        # "this reply is a redundant confirmation"). Replies without complete_phase
        # (e.g. the child returned a question instead of dispatching) are still echoed.
        completed_phase = (
            tool_uses is not None
            and f"mcp__{MCP_SERVER_NAME}__complete_phase" in tool_uses
        )
        if completed_phase:
            log.debug(
                "runner.echo_skipped_complete_phase",
                topic=key.slug(),
            )
            return
        if self.db_pool is None:
            return
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT pc.id AS parent_id,
                              ps.name AS parent_stream,
                              pc.topic_name AS parent_topic
                         FROM messaging.conversations c
                         JOIN messaging.streams s ON s.id = c.stream_id
                         JOIN messaging.conversations pc ON pc.id = c.parent_conv_id
                         JOIN messaging.streams ps ON ps.id = pc.stream_id
                        WHERE s.name = $1 AND c.topic_name = $2""",
                    key.stream, key.topic,
                )
        except Exception:
            log.exception("runner.parent_lookup_failed", topic=key.slug())
            return
        if row is None:
            return
        agent_label = self.agent_name or key.stream
        forwarded = (
            f"🗨️ **Reply from `{agent_label}`** "
            f"(`{key.stream}` / `{key.topic}`)\n\n{content}"
        )
        try:
            await self.broker_client.send_message(
                row["parent_stream"], row["parent_topic"], forwarded,
                client_id=f"echo-{reply_uid}",
                kind="echo",
            )
            log.info(
                "runner.forwarded_to_parent",
                child_topic=key.slug(),
                parent_stream=row["parent_stream"],
                parent_topic=row["parent_topic"],
            )
        except Exception:
            log.exception(
                "runner.forward_to_parent_failed",
                child_topic=key.slug(),
                parent_stream=row["parent_stream"],
                parent_topic=row["parent_topic"],
            )
