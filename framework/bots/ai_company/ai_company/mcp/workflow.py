"""WorkflowManager — generic protocol for multi-phase tasks.

This module is **vocabulary-agnostic**: it does not know what "triage",
"analysis", "coordinator", etc. are. The instance (company) declares its steps,
default agents, artifacts and transitions in `company/workflows.yaml`. The
framework loads it at runtime, validates against the declaration and resolves
routing.

The framework knows ONLY:
  - Tasks, with a kebab-case slug, live in Postgres (schema `tasks`).
  - Artifacts (the .md files produced in each phase) stay in
    `company/tasks/<slug>/` — agents write them via the CLI Write tool,
    the PWA reads and renders them.
  - A phase has a `step` (free string, defined by the instance).
  - Terminal transitions with fixed semantics: `done` | `halt` | `human_review`
    — these names are part of the protocol because they change status and
    dispatch in the reactor. Instances cannot redefine them.
  - `metadata.workflow: <name>` selects which company workflow governs
    the task. If there is no workflows.yaml or the name does not match, the
    framework runs in **permissive mode** — accepts any step but does not
    resolve a default agent nor validate transitions.

Events in Postgres (orchestrator.events); the reactor consumes them via LISTEN.
tasks.* schema added by migration 006 (D-53).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import asyncpg
import yaml

from ..log import get_logger

log = get_logger(__name__)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Protocol terminals — part of the framework, not of the instance.
# Each one has distinct semantics for the reactor:
#   done          => status=done       (success)
#   halt          => status=blocked    (pause — a human unblocks)
#   human_review  => status=human_review (escalation — a human decides direction)
TERMINALS: set[str] = {"done", "halt", "human_review"}


class WorkflowError(ValueError):
    """Protocol error — returned as JSONRPC_INVALID_PARAMS."""


# ---------- Workflow definition loaded from workflows.yaml ----------


@dataclass(frozen=True)
class StepDef:
    """Declaration of a step according to the instance."""
    name: str
    agent: str | None               # None => the call must pass next_agent
    artifact: str | None            # default file produced in this step
    next: frozenset[str]            # steps/terminals valid as next
    # When True, on entering this step the framework clears the claude_session_id
    # of the target conv (stream, topic) — the next spawn runs `claude -p` without
    # `--resume`, with a fresh context window. The step's `instructions`
    # go through append-system-prompt as usual (dynamic build). Default
    # False: keeps the session via --resume (legacy behavior).
    fresh_session: bool = False


@dataclass(frozen=True)
class WorkflowDef:
    name: str
    initial_step: str
    steps: dict[str, StepDef] = field(default_factory=dict)
    # Agent that orchestrates this workflow — owner of the supervisor conv that
    # `backlog_promote` creates. NULL => fallback to `initial_step.agent`
    # (compat with pre-orchestrator workflows). Unlike
    # `initial_step.agent`: the orchestrator may not run the first phase
    # but is still the root of the task's convs. E.g. a deploy workflow
    # has orchestrator=`deploy-lead`, but the first phase may go
    # straight to `test-runner`.
    orchestrator: str | None = None

    def step(self, name: str) -> StepDef | None:
        return self.steps.get(name)


class WorkflowRegistry:
    """Loads the instance's workflows.yaml and returns a WorkflowDef by name.

    Reads only at call time (no hit-the-disk-once cache) —
    reconcile/reload is cheap and lets workflows.yaml be edited without a restart.
    """

    def __init__(self, workflows_path: Path):
        self.workflows_path = workflows_path

    def load(self) -> dict[str, WorkflowDef]:
        if not self.workflows_path.exists():
            return {}
        raw = yaml.safe_load(self.workflows_path.read_text(encoding="utf-8")) or {}
        wfs_raw = raw.get("workflows") or {}
        out: dict[str, WorkflowDef] = {}
        for wf_name, wf_body in wfs_raw.items():
            if not isinstance(wf_body, dict):
                continue
            steps_raw = wf_body.get("steps") or {}
            steps: dict[str, StepDef] = {}
            for s_name, s_body in steps_raw.items():
                if not isinstance(s_body, dict):
                    continue
                steps[s_name] = StepDef(
                    name=s_name,
                    agent=s_body.get("agent"),
                    artifact=s_body.get("artifact"),
                    next=frozenset(s_body.get("next") or []),
                    fresh_session=bool(s_body.get("fresh_session", False)),
                )
            initial_step_name = str(wf_body.get("initial_step") or "")
            # orchestrator: dedicated field; without it, fall back to the
            # initial_step agent (compat with old workflows).
            orchestrator = wf_body.get("orchestrator")
            if not orchestrator and initial_step_name in steps:
                orchestrator = steps[initial_step_name].agent
            out[wf_name] = WorkflowDef(
                name=wf_name,
                initial_step=initial_step_name,
                steps=steps,
                orchestrator=orchestrator,
            )
        return out

    def get(self, name: str | None) -> WorkflowDef | None:
        if not name:
            return None
        return self.load().get(name)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------- WorkflowManager ----------


class WorkflowManager:
    """Generic protocol — delegates the taxonomy to WorkflowRegistry.

    Persistence in Postgres (tasks.tasks, tasks.phases, tasks.worktrees).
    `company_dir` is still passed to resolve `workflows.yaml` and the
    artifacts directory `company/tasks/<slug>/` (where the .md files live).
    """

    def __init__(self, company_dir: Path, db_pool: asyncpg.Pool):
        self.company_dir = company_dir
        self.tasks_dir = company_dir / "tasks"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self._pool = db_pool
        self.registry = WorkflowRegistry(company_dir / "workflows.yaml")

    # ---------- helpers ----------

    def _task_dir(self, slug: str) -> Path:
        """Filesystem path of the task's artifacts (.md) — metadata lives in the DB."""
        return self.tasks_dir / slug

    async def _load_task_row(self, conn: asyncpg.Connection, slug: str) -> dict[str, Any] | None:
        row = await conn.fetchrow(
            """SELECT id, slug, title, workflow, status, current_step, current_agent,
                      complexity, impact, difficulty, origin, origin_stream, origin_topic,
                      blocked_reason, metadata_extra, archived_at, created_at, updated_at
                 FROM tasks.tasks WHERE slug = $1""",
            slug,
        )
        return dict(row) if row else None

    async def _load_phases(self, conn: asyncpg.Connection, task_id: int) -> list[dict[str, Any]]:
        rows = await conn.fetch(
            """SELECT idx, step, agent, started_at, completed_at, artifact, summary
                 FROM tasks.phases WHERE task_id = $1 ORDER BY idx ASC""",
            task_id,
        )
        return [dict(r) for r in rows]

    async def _load_worktrees(self, conn: asyncpg.Connection, task_id: int) -> dict[str, dict]:
        rows = await conn.fetch(
            """SELECT repo, branch, path, created_at
                 FROM tasks.worktrees WHERE task_id = $1""",
            task_id,
        )
        return {
            r["repo"]: {
                "repo": r["repo"],
                "branch": r["branch"],
                "path": r["path"],
                "registered_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        }

    def _resolve_workflow(self, workflow_name: str | None) -> WorkflowDef | None:
        return self.registry.get(workflow_name)

    def _validate_transition(
        self,
        wf: WorkflowDef | None,
        current_step: str | None,
        next_: str,
    ) -> None:
        """Validates the transition against the declared workflow. Permissive mode
        (accepts everything) if there is no wf or the step is not declared."""
        if wf is None or current_step is None:
            return  # permissive mode
        step = wf.step(current_step)
        if step is None:
            return  # step missing from the wf — permissive
        if next_ not in step.next:
            allowed = sorted(step.next)
            raise WorkflowError(
                f"Invalid transition in workflow '{wf.name}': from '{current_step}' "
                f"you can only go to {allowed}. Attempted '{next_}'."
            )

    def _resolve_next_agent(
        self,
        wf: WorkflowDef | None,
        next_: str,
        next_agent_override: str | None,
    ) -> tuple[str | None, bool]:
        """Returns (agent_resolved_or_override, is_terminal).

        Resolution order:
          1. terminal → (None, True).
          2. next_agent_override always wins.
          3. wf.step(next_).agent if declared.
          4. error: override required.
        """
        if next_ in TERMINALS:
            return None, True
        if next_agent_override:
            return next_agent_override, False
        if wf is not None:
            step = wf.step(next_)
            if step is not None:
                if step.agent:
                    return step.agent, False
                raise WorkflowError(
                    f"Step '{next_}' in workflow '{wf.name}' has no default agent "
                    "declared. Pass an explicit next_agent."
                )
        # No workflow declared or step undefined — override required.
        raise WorkflowError(
            f"Could not resolve agent for step '{next_}'. Declare it in "
            "company/workflows.yaml (with field `agent`) or pass an explicit next_agent."
        )

    def _expected_artifact_for(self, wf: WorkflowDef | None, step_name: str) -> str | None:
        if wf is None:
            return None
        s = wf.step(step_name)
        return s.artifact if s else None

    @staticmethod
    def _resolve_handoff_topic(
        *,
        explicit_topic: str | None,
        next_agent: str | None,
        task_slug: str,
        origin_stream: str | None,
        origin_topic: str | None,
        standalone: bool = False,
    ) -> str:
        """Picks the topic where the handoff will be posted.

        Priority:
          1. `explicit_topic` (override passed by the caller).
          2. `standalone=True` -> always `task-<slug>`. Fan-out calls for
             isolation by design; rule (3) below does not apply because
             the human *wants* a new conv.
          3. If the handoff returns to the origin agent (the one that opened
             the task), use the topic where the human asked — keeps human and
             agent in the same thread throughout the task's lifecycle.
          4. Fallback `task-<slug>`.

        Rule (3) avoids the failure mode where the return to the origin agent
        lands in a new `task-<slug>` topic the human does not even know exists;
        if the agent forgets to call `ask_human`, the message goes
        unnoticed. Post directly in the conversation that is already open.

        But in the ops case (mega-agent, agent==origin_stream always)
        rule (3) would fire on every handoff, fan-out included, and
        collapse the N parallel tasks into the original conv. `standalone`
        signals that context and disables the fold-back.
        """
        if explicit_topic:
            return explicit_topic
        if standalone:
            return f"task-{task_slug}"
        if (
            next_agent
            and origin_stream
            and origin_topic
            and next_agent == origin_stream
        ):
            return origin_topic
        return f"task-{task_slug}"

    # ---------- main API ----------

    async def complete_phase(
        self,
        *,
        task_slug: str,
        artifact: str,
        summary: str,
        next_: str,
        agent_name: str,
        next_agent: str | None = None,
        next_topic: str | None = None,
        origin_stream: str | None = None,
        origin_topic: str | None = None,
        title: str | None = None,
        workflow: str | None = None,
        complexity: str | None = None,
        impact: str | None = None,
        difficulty: str | None = None,
        origin: str | None = None,
        baseline: dict[str, str] | None = None,
        standalone: bool = False,
    ) -> dict[str, Any]:
        """Records the end of the current step and triggers the handoff to the next step.

        The first call creates the task (the schema allows upsert). `current_step`
        is read from the DB. If missing, it comes from `workflow.initial_step`.
        Permissive mode (no declared workflow) falls back to 'start'.

        All in one transaction: UPSERT tasks, INSERT phases, INSERT
        orchestrator.events. If anything fails, atomic rollback.
        """
        if not SLUG_RE.match(task_slug):
            raise WorkflowError(
                f"invalid task_slug: {task_slug!r}. Use kebab-case without accents."
            )

        # Artifacts stay on the filesystem — complete_phase requires the file
        # to exist before recording the phase in the DB (avoids orphan phases).
        task_dir = self._task_dir(task_slug)
        task_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = task_dir / artifact
        if not artifact_path.exists():
            raise FileNotFoundError(
                f"Artifact '{artifact}' does not exist in {task_dir}. "
                "Create the file BEFORE calling complete_phase."
            )

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                task_row = await self._load_task_row(conn, task_slug)
                is_first_call = task_row is None

                # Workflow comes from the DB (if the task exists) or from the argument (create).
                wf_name = (task_row or {}).get("workflow") or workflow
                wf = self._resolve_workflow(wf_name)

                # Resolve current_step.
                # Order: DB > last completed phase > wf initial_step > 'start'.
                #
                # The fallback to "last completed phase" is critical for tasks that
                # already went through a non-`done` terminal (halt/human_review). In
                # those cases `current_step` in the DB is NULL (UPSERT sets NULL for
                # is_terminal); using `initial_step` would make a subsequent
                # `complete_phase` (e.g. an agent moving to the closing step after
                # human_review) record `from_step=<initial step>` in the payload,
                # producing a handoff with a wrong "Previous step: <initial step>".
                current_step = (task_row or {}).get("current_step")
                if not current_step and task_row is not None:
                    last_done_step = await conn.fetchval(
                        """SELECT step FROM tasks.phases
                            WHERE task_id = $1 AND completed_at IS NOT NULL
                            ORDER BY idx DESC LIMIT 1""",
                        task_row["id"],
                    )
                    if last_done_step:
                        current_step = last_done_step
                if not current_step:
                    current_step = wf.initial_step if wf and wf.initial_step else "start"

                self._validate_transition(wf, current_step, next_)
                resolved_agent, is_terminal = self._resolve_next_agent(wf, next_, next_agent)

                now = datetime.now(tz=timezone.utc)

                # Determine started_at of the completed phase. If there is already an
                # open phase (completed_at NULL), use its started_at. Otherwise,
                # use the task's created_at (first phase).
                in_flight = await conn.fetchrow(
                    """SELECT id, idx, started_at FROM tasks.phases
                        WHERE task_id = (SELECT id FROM tasks.tasks WHERE slug = $1)
                          AND completed_at IS NULL
                        ORDER BY idx DESC LIMIT 1""",
                    task_slug,
                )
                started_at = (
                    in_flight["started_at"] if in_flight else
                    (task_row["created_at"] if task_row else now)
                )

                # UPSERT task. Descriptive attributes (origin_*, complexity, impact,
                # difficulty, origin) are only written if not set yet — avoids
                # accidental overwrites on subsequent calls.
                task_id = await conn.fetchval(
                    """INSERT INTO tasks.tasks
                        (slug, title, workflow, status, current_step, current_agent,
                         origin_stream, origin_topic,
                         complexity, impact, difficulty, origin)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                       ON CONFLICT (slug) DO UPDATE SET
                         status        = EXCLUDED.status,
                         current_step  = EXCLUDED.current_step,
                         current_agent = EXCLUDED.current_agent,
                         workflow      = COALESCE(tasks.tasks.workflow, EXCLUDED.workflow),
                         title         = COALESCE(tasks.tasks.title, EXCLUDED.title),
                         origin_stream = COALESCE(tasks.tasks.origin_stream, EXCLUDED.origin_stream),
                         origin_topic  = COALESCE(tasks.tasks.origin_topic, EXCLUDED.origin_topic),
                         complexity    = COALESCE(tasks.tasks.complexity, EXCLUDED.complexity),
                         impact        = COALESCE(tasks.tasks.impact, EXCLUDED.impact),
                         difficulty    = COALESCE(tasks.tasks.difficulty, EXCLUDED.difficulty),
                         origin        = COALESCE(tasks.tasks.origin, EXCLUDED.origin),
                         blocked_reason = CASE
                           WHEN EXCLUDED.status IN ('blocked','human_review') THEN EXCLUDED.current_step
                           ELSE NULL
                         END
                       RETURNING id""",
                    task_slug,
                    title or (task_row["title"] if task_row else task_slug),
                    wf_name,
                    (
                        "done" if next_ == "done" else
                        "blocked" if next_ == "halt" else
                        "human_review" if next_ == "human_review" else
                        "in_progress"
                    ),
                    None if is_terminal else next_,
                    None if is_terminal else resolved_agent,
                    origin_stream,
                    origin_topic,
                    complexity,
                    impact,
                    difficulty,
                    origin,
                )

                # baseline: merge into metadata_extra.baseline (dict repo->sha).
                # Additive: new repos are added; does NOT overwrite an existing sha
                # (guarantee: "baseline is immutable per repo").
                if baseline:
                    for repo, sha in baseline.items():
                        if not (repo and sha):
                            continue
                        await conn.execute(
                            """UPDATE tasks.tasks
                                  SET metadata_extra = jsonb_set(
                                        COALESCE(metadata_extra, '{}'::jsonb),
                                        ARRAY['baseline', $2],
                                        to_jsonb($3::text),
                                        true
                                      )
                                WHERE id = $1
                                  AND COALESCE(metadata_extra->'baseline'->>$2, '') = ''""",
                            task_id, repo, sha,
                        )

                # Detailed blocked_reason (the CASE above uses current_step as a placeholder).
                if is_terminal and next_ == "halt":
                    await conn.execute(
                        "UPDATE tasks.tasks SET blocked_reason = $2 WHERE id = $1",
                        task_id, f"halt by {agent_name} at step '{current_step}'",
                    )
                elif is_terminal and next_ == "human_review":
                    await conn.execute(
                        "UPDATE tasks.tasks SET blocked_reason = $2 WHERE id = $1",
                        task_id, f"human_review requested by {agent_name} at step '{current_step}'",
                    )

                # D-102: terminal `done` -> the linked backlog item moves to
                # 'done'. The user asked for a separate kanban column to
                # tell what was already delivered from what is still running
                # (before, both stayed in 'promoted'). NOOP if the task did not
                # come from the backlog (promoted_task_slug NULL or row missing).
                if is_terminal and next_ == "done":
                    await conn.execute(
                        """UPDATE tasks.backlog
                              SET status = 'done', updated_at = now()
                            WHERE promoted_task_slug = $1
                              AND status = 'promoted'""",
                        task_slug,
                    )

                # Close the in-flight phase if it exists; otherwise create a new phase
                # already completed (atomically represents completion + archiving).
                if in_flight is not None:
                    await conn.execute(
                        """UPDATE tasks.phases
                              SET completed_at = $2, artifact = $3, summary = $4,
                                  agent = COALESCE(agent, $5), step = $6
                            WHERE id = $1""",
                        in_flight["id"], now, artifact, summary, agent_name, current_step,
                    )
                    last_idx = in_flight["idx"]
                else:
                    # Append after the last existing idx (-1 when empty,
                    # via COALESCE; +1 gives 0 for the first phase).
                    # NOTE: do not use `int(x or -1)` here — `0 or -1 == -1` in
                    # Python (0 is falsy), which re-inserts idx=0 on tasks
                    # with exactly one phase and breaks the unique constraint.
                    last_idx_row = await conn.fetchval(
                        "SELECT COALESCE(MAX(idx), -1) FROM tasks.phases WHERE task_id = $1",
                        task_id,
                    )
                    last_idx = int(last_idx_row) + 1
                    await conn.execute(
                        """INSERT INTO tasks.phases
                            (task_id, idx, step, agent, started_at, completed_at, artifact, summary)
                           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
                        task_id, last_idx, current_step, agent_name, started_at, now, artifact, summary,
                    )

                # Create the in-flight phase for the next step (if not terminal).
                if not is_terminal:
                    await conn.execute(
                        """INSERT INTO tasks.phases
                            (task_id, idx, step, agent, started_at)
                           VALUES ($1, $2, $3, $4, $5)""",
                        task_id, last_idx + 1, next_, resolved_agent, now,
                    )

                # Snapshot the origin (so the reactor posts the terminal there).
                task_final = await self._load_task_row(conn, task_slug)

                # Payload + event for the reactor to consume.
                next_artifact_default = self._expected_artifact_for(wf, next_) if not is_terminal else None
                resolved_origin_stream = task_final["origin_stream"] if task_final else None
                resolved_origin_topic = task_final["origin_topic"] if task_final else None
                resolved_next_topic = None if is_terminal else self._resolve_handoff_topic(
                    explicit_topic=next_topic,
                    next_agent=resolved_agent,
                    task_slug=task_slug,
                    origin_stream=resolved_origin_stream,
                    origin_topic=resolved_origin_topic,
                    standalone=bool(standalone),
                )
                # fresh_session of the target step: the reactor uses it to clear
                # the conv's (stream, topic) claude_session_id before posting
                # the handoff. Ignored for terminals (no dispatch to do).
                next_fresh_session = False
                if not is_terminal and wf is not None:
                    next_step_def = wf.step(next_)
                    if next_step_def is not None:
                        next_fresh_session = next_step_def.fresh_session
                payload = {
                    "task_slug": task_slug,
                    "from_step": current_step,
                    "from_agent": agent_name,
                    "artifact": artifact,
                    "summary": summary,
                    "next": next_,
                    "next_agent": resolved_agent,
                    "next_topic": resolved_next_topic,
                    "next_artifact": next_artifact_default,
                    "next_fresh_session": next_fresh_session,
                    "standalone": bool(standalone),
                    "origin_stream": resolved_origin_stream,
                    "origin_topic":  resolved_origin_topic,
                    "workflow": task_final["workflow"] if task_final else None,
                }
                event_id = await conn.fetchval(
                    """INSERT INTO orchestrator.events (emitted_by, event_type, task_slug, payload)
                       VALUES ($1, 'phase_complete', $2, $3::jsonb)
                       RETURNING id""",
                    agent_name, task_slug, json.dumps(payload),
                )

        log.info(
            "workflow.complete_phase",
            task_slug=task_slug,
            from_step=current_step,
            from_agent=agent_name,
            artifact=artifact,
            next=next_,
            next_agent=resolved_agent,
            event_id=event_id,
            is_first_call=is_first_call,
        )

        if is_terminal:
            guidance = {
                "done": (
                    "Task closed successfully. The human will be notified via "
                    "TERMINAL_NOTIFY_STREAM and also in the origin conversation "
                    "(if registered). If you want to post an extra summary in "
                    "the current conversation, use notify_human."
                ),
                "halt": (
                    "Task paused (status=blocked). The human needs to unblock. "
                    "Use notify_human or ask_human to signal the reason."
                ),
                "human_review": (
                    "Task escalated to human (status=human_review). Use "
                    "ask_human now if the review requires a blocking answer."
                ),
            }[next_]
        else:
            guidance = (
                f"Phase '{current_step}' recorded. Handoff to '{resolved_agent}' "
                f"(step '{next_}') dispatched — the reactor will post the request at "
                f"#{resolved_agent}/{payload['next_topic']}. You do NOT need to post "
                "a separate message announcing the dispatch; it's already done. "
                "If you want to notify the human of the status, use notify_human."
            )
            # Extra warning when the return lands in the origin topic (the human is
            # watching this conversation live). Without `ask_human` / `complete_phase`
            # the run ends silently but the message is already in the open thread —
            # still, the agent should block properly.
            if (
                resolved_origin_stream
                and resolved_origin_topic
                and resolved_agent == resolved_origin_stream
                and payload["next_topic"] == resolved_origin_topic
            ):
                guidance += (
                    " This handoff returns to the human's origin topic "
                    f"(#{resolved_origin_stream}/{resolved_origin_topic}); "
                    "if you need their answer before proceeding, use "
                    "`ask_human(blocking=true)` explicitly."
                )

        return {
            "event_id": event_id,
            "task_slug": task_slug,
            "artifact_path": str(artifact_path),
            "from_step": current_step,
            "next": next_,
            "next_step": None if is_terminal else next_,
            "next_agent": resolved_agent,
            "next_artifact": next_artifact_default,
            "status": (
                "done" if next_ == "done" else
                "blocked" if next_ == "halt" else
                "human_review" if next_ == "human_review" else
                "in_progress"
            ),
            "guidance": guidance,
        }

    async def init_repo(
        self,
        *,
        name: str,
        agent_name: str,
    ) -> dict[str, Any]:
        """Create a new git repo at /workspace/repos/<name>/ (one empty commit on `main`).

        The agent's repos mount is read-only (D-115), so the web container
        creates the repo (POST /api/repos/init); see web/app/repos.py for the
        layout that keeps the new repo's git dir writable from here.
        Idempotent: an existing repo is returned untouched.
        """
        if not SLUG_RE.match(name):
            raise WorkflowError(f"invalid repo name (use kebab-case): {name!r}")
        broker_url = os.environ.get("BROKER_URL", "http://web:8090").rstrip("/")
        token = os.environ.get("BROKER_TOKEN", "")
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(
                f"{broker_url}/api/repos/init",
                json={"name": name},
                headers={"Authorization": f"Bearer {token}"},
            ) as r:
                if r.status == 422:
                    raise WorkflowError((await r.json()).get("detail", "invalid repo name"))
                if r.status >= 400:
                    raise RuntimeError(f"repo init failed: HTTP {r.status} {(await r.text())[:300]}")
                result = await r.json()
        repo_dir = result["path"]
        sha = result["head_sha"]
        log.info("workflow.init_repo", repo=name, created=result["created"], head_sha=sha, agent=agent_name)
        if result["created"]:
            guidance = (
                f"Repo '{name}' created at {repo_dir} (branch main, SHA {sha[:8]}). "
                "Now call create_worktree to get an isolated worktree for your task."
            )
        else:
            guidance = (
                f"Repo '{name}' already exists at {repo_dir}. "
                "Call create_worktree to get a worktree to work in."
            )
        return {**result, "guidance": guidance}

    async def create_worktree(
        self,
        *,
        task_slug: str,
        repo: str,
        baseline_sha: str | None = None,
        branch: str | None = None,
        agent_name: str,
    ) -> dict[str, Any]:
        """Creates an isolated worktree + records it in the DB, atomically.

        Resolves baseline_sha when not provided (the remote's default-branch
        HEAD). Creates it at `${WORKTREES_DIR}/<repo>/<slug>/`, outside the
        `repos/<repo>/` tree — worktrees never pollute the canonical repo's status.

        Idempotent: if (task_id, repo, branch) already exists in the DB AND the
        path on disk matches a valid worktree, returns without recreating. If the
        path is gone or HEAD does not match, recreates.

        Baseline resolution:
          - an explicit `baseline_sha` always takes priority;
          - otherwise, `git fetch --quiet origin` + `git rev-parse origin/<default>`,
            where `<default>` comes from `git symbolic-ref refs/remotes/origin/HEAD`.
        """
        if not SLUG_RE.match(task_slug):
            raise WorkflowError(f"invalid task_slug: {task_slug!r}")
        if not repo:
            raise WorkflowError("repo is required.")

        repos_root = Path(os.environ.get("WORKSPACE_REPOS", "/workspace/repos"))
        worktrees_root = Path(os.environ.get("WORKTREES_DIR", "/workspace/worktrees"))
        repo_dir = repos_root / repo
        if not (repo_dir / ".git").exists():
            raise WorkflowError(
                f"Repo {repo!r} does not exist in {repos_root}/ (looked for: {repo_dir}/.git). "
                "Check that the repo is cloned in instance/repos/ and that the mount "
                "reaches the container."
            )

        branch_final = branch or f"task/{task_slug}"
        wt_path = worktrees_root / repo / task_slug

        async with self._pool.acquire() as conn:
            task_id = await conn.fetchval(
                "SELECT id FROM tasks.tasks WHERE slug = $1", task_slug,
            )
            if task_id is None:
                raise WorkflowError(
                    f"Task {task_slug!r} does not exist. Run complete_phase first to create it."
                )

        # Resolve the baseline before touching disk — if it fails, nothing changed.
        if not baseline_sha:
            baseline_sha = await self._resolve_default_baseline(repo_dir)
        elif not re.fullmatch(r"[0-9a-f]{7,40}", baseline_sha):
            raise WorkflowError(
                f"baseline_sha {baseline_sha!r} does not look like a hex SHA (7-40 chars)."
            )

        # Idempotency: is there already a valid worktree at this path on the branch?
        existing = await self._existing_worktree(repo_dir, wt_path, branch_final)
        if existing is None:
            # Create. If something along the way is dirty (path exists but is not a
            # worktree, or the branch already exists loose), abort with a msg — no guessing.
            worktrees_root.mkdir(parents=True, exist_ok=True)
            (worktrees_root / repo).mkdir(parents=True, exist_ok=True)
            await self._git_worktree_add(repo_dir, wt_path, branch_final, baseline_sha)

        wt_path_str = str(wt_path)
        async with self._pool.acquire() as conn:
            # Upsert by (task_id, repo, branch). Path is updated if it changed.
            await conn.execute(
                """INSERT INTO tasks.worktrees (task_id, repo, branch, path)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (task_id, repo, branch) DO UPDATE SET path = EXCLUDED.path""",
                task_id, repo, branch_final, wt_path_str,
            )
            # Store baseline_sha in metadata_extra (appended per repo).
            await conn.execute(
                """UPDATE tasks.tasks
                      SET metadata_extra = jsonb_set(
                            COALESCE(metadata_extra, '{}'::jsonb),
                            ARRAY['baseline', $2],
                            to_jsonb($3::text),
                            true
                          )
                    WHERE id = $1""",
                task_id, repo, baseline_sha,
            )
        log.info(
            "workflow.create_worktree",
            task_slug=task_slug, repo=repo, path=wt_path_str, branch=branch_final,
            baseline_sha=baseline_sha, reused=existing is not None, agent=agent_name,
        )
        guidance = (
            f"Worktree ready at {wt_path_str}. `cd {wt_path_str}` and work "
            "from there — do not edit /workspace/repos/<repo>/ directly. All "
            "commits must come from branch '" + branch_final + "'."
        )
        if existing is not None:
            guidance = "Existing worktree reused. " + guidance
        return {
            "task_slug": task_slug,
            "worktree": {
                "repo": repo, "branch": branch_final, "path": wt_path_str,
                "baseline_sha": baseline_sha, "created_by": agent_name,
                "reused": existing is not None,
            },
            "guidance": guidance,
        }

    async def _resolve_default_baseline(self, repo_dir: Path) -> str:
        """Resolves the SHA of the remote default branch.

        Flow:
          1. `git fetch --quiet origin` — ensures up-to-date refs.
          2. `git symbolic-ref refs/remotes/origin/HEAD` -> `refs/remotes/origin/<default>`.
          3. `git rev-parse refs/remotes/origin/<default>` -> SHA.

        If origin/HEAD is not set locally (old clone), it first tries
        `git remote set-head origin --auto`. If everything fails, raises an
        explanatory error — we do NOT guess `main` or `master`.

        A repo with no `origin` remote (e.g. one made by init_repo) has no
        remote default branch: its local HEAD is the baseline.
        """
        remotes = await asyncio.create_subprocess_exec(
            "git", "-C", str(repo_dir), "remote",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        remotes_out, _ = await remotes.communicate()
        if remotes.returncode == 0 and "origin" not in remotes_out.decode().split():
            head = await asyncio.create_subprocess_exec(
                "git", "-C", str(repo_dir), "rev-parse", "HEAD",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            head_out, head_err = await head.communicate()
            if head.returncode != 0:
                raise WorkflowError(
                    f"rev-parse HEAD failed at {repo_dir}: "
                    + head_err.decode("utf-8", "replace").strip()
                )
            return head_out.decode().strip()

        fetch = await asyncio.create_subprocess_exec(
            "git", "-C", str(repo_dir), "fetch", "--quiet", "origin",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await fetch.communicate()
        if fetch.returncode != 0:
            raise WorkflowError(
                f"git fetch failed at {repo_dir}: "
                + stderr.decode("utf-8", "replace").strip()
            )

        head_ref = await self._git_symbolic_ref_head(repo_dir)
        if head_ref is None:
            # Try to auto-detect and set origin/HEAD locally.
            auto = await asyncio.create_subprocess_exec(
                "git", "-C", str(repo_dir), "remote", "set-head", "origin", "--auto",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await auto.communicate()
            head_ref = await self._git_symbolic_ref_head(repo_dir)
        if head_ref is None:
            raise WorkflowError(
                f"Could not resolve default branch at {repo_dir} "
                "(`origin/HEAD` missing). Pass an explicit baseline_sha."
            )

        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(repo_dir), "rev-parse", head_ref,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise WorkflowError(
                f"rev-parse {head_ref} failed: "
                + stderr.decode("utf-8", "replace").strip()
            )
        sha = stdout.decode("utf-8", "replace").strip()
        if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
            raise WorkflowError(f"rev-parse returned invalid SHA: {sha!r}")
        return sha

    async def _git_symbolic_ref_head(self, repo_dir: Path) -> str | None:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(repo_dir),
            "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        ref = stdout.decode("utf-8", "replace").strip()
        return ref or None

    async def _existing_worktree(
        self, repo_dir: Path, wt_path: Path, branch: str,
    ) -> dict[str, Any] | None:
        """Checks whether a valid worktree already exists at the expected path + branch.

        Returns a dict with info when it matches; None otherwise.
        """
        if not wt_path.exists():
            return None
        # `git worktree list --porcelain` — parse it to find the entry with this path.
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(repo_dir), "worktree", "list", "--porcelain",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        entries: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in stdout.decode("utf-8", "replace").splitlines():
            if not line.strip():
                if current:
                    entries.append(current); current = {}
                continue
            k, _, v = line.partition(" ")
            current[k] = v
        if current:
            entries.append(current)
        wt_abs = str(wt_path.resolve()) if wt_path.exists() else str(wt_path)
        for e in entries:
            if e.get("worktree") == wt_abs or e.get("worktree") == str(wt_path):
                ref = e.get("branch", "")
                expected = f"refs/heads/{branch}"
                if ref == expected:
                    return {"path": e["worktree"], "branch": branch}
                # Path exists but on a different branch — return None to force an error
                # in git worktree add (which will fail with "already exists").
                return None
        return None

    async def _git_worktree_add(
        self, repo_dir: Path, wt_path: Path, branch: str, baseline_sha: str,
    ) -> None:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(repo_dir),
            "worktree", "add", "-b", branch, str(wt_path), baseline_sha,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", "replace").strip()
            out = stdout.decode("utf-8", "replace").strip()
            raise WorkflowError(
                f"git worktree add failed (rc={proc.returncode}): {err or out}"
            )

    async def cleanup_worktrees(
        self,
        *,
        task_slug: str,
        agent_name: str,
    ) -> dict[str, Any]:
        """Removes all worktrees registered for a task.

        Flow per worktree:
          1. `git -C <canonical_repo> worktree remove --force <path>` — removes
             the git record + deletes the folder (when the path still exists on disk).
          2. `git -C <canonical_repo> worktree prune` — ensures orphan git
             records go away even if the folder no longer existed.
          3. `shutil.rmtree(path)` as a backstop — in case the worktree remove
             failed for some reason but the folder persisted.
          4. `DELETE FROM tasks.worktrees WHERE ...` — only deletes the row if the
             path is really gone from disk.

        Idempotent: running again on an already-clean task does not error; returns
        removed=[] and an informative guidance.

        Failures are not masked: if a worktree cannot be removed, it is
        returned in `failed` and the caller (typically the agent closing the
        task) must escalate via `complete_phase(next='human_review')`.

        The canonical repo is resolved as `/workspace/repos/<repo>` — the
        framework's bind mount convention. If the instance uses a different
        prefix, this logic must change with it.
        """
        if not SLUG_RE.match(task_slug):
            raise WorkflowError(f"invalid task_slug: {task_slug!r}")

        async with self._pool.acquire() as conn:
            task_id = await conn.fetchval(
                "SELECT id FROM tasks.tasks WHERE slug = $1", task_slug,
            )
            if task_id is None:
                raise WorkflowError(f"Task {task_slug!r} does not exist.")
            rows = await conn.fetch(
                "SELECT repo, branch, path FROM tasks.worktrees WHERE task_id = $1",
                task_id,
            )

        if not rows:
            log.info(
                "workflow.cleanup_worktrees.noop",
                task_slug=task_slug, agent=agent_name,
            )
            return {
                "task_slug": task_slug,
                "removed": [],
                "failed": [],
                "guidance": (
                    "No worktrees registered for this task — nothing to clean up. "
                    "You may proceed."
                ),
            }

        removed: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []

        for r in rows:
            repo = r["repo"]
            path = r["path"]
            branch = r["branch"]
            canonical_repo = f"/workspace/repos/{repo}"
            errors: list[str] = []

            # 1) git worktree remove --force
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git", "-C", canonical_repo,
                    "worktree", "remove", "--force", path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode != 0:
                    errors.append(
                        f"worktree remove rc={proc.returncode}: "
                        + stderr.decode("utf-8", "replace").strip()
                    )
            except FileNotFoundError:
                errors.append("git binary missing in the container")
            except Exception as e:  # noqa: BLE001
                errors.append(f"worktree remove exc: {e}")

            # 2) git worktree prune (cleans orphan records)
            try:
                prune_proc = await asyncio.create_subprocess_exec(
                    "git", "-C", canonical_repo, "worktree", "prune",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await prune_proc.communicate()
            except Exception as e:  # noqa: BLE001
                errors.append(f"worktree prune exc: {e}")

            # 3) rmtree backstop — if the path persists, remove it manually
            if os.path.exists(path):
                try:
                    shutil.rmtree(path)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"rmtree {path} failed: {e}")

            if not os.path.exists(path):
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        """DELETE FROM tasks.worktrees
                            WHERE task_id = $1 AND repo = $2 AND branch = $3""",
                        task_id, repo, branch,
                    )
                removed.append({
                    "repo": repo, "path": path, "branch": branch,
                    "warnings": errors or None,
                })
            else:
                failed.append({
                    "repo": repo, "path": path, "branch": branch,
                    "errors": errors or ["path persists after remove+prune+rmtree"],
                })

        log.info(
            "workflow.cleanup_worktrees",
            task_slug=task_slug, agent=agent_name,
            removed_count=len(removed), failed_count=len(failed),
        )

        if failed:
            guidance = (
                f"Partial cleanup: {len(removed)} removed, {len(failed)} "
                "failed. Do NOT close the task — investigate the errors, and if "
                "you can't resolve them, escalate via complete_phase(next='human_review')."
            )
        else:
            guidance = (
                f"Cleanup complete: {len(removed)} worktree(s) removed and "
                "deleted from tasks.worktrees. You may call "
                "complete_phase(next='done')."
            )

        return {
            "task_slug": task_slug,
            "removed": removed,
            "failed": failed,
            "guidance": guidance,
        }

    async def get_task_state(self, task_slug: str) -> dict[str, Any]:
        if not SLUG_RE.match(task_slug):
            raise WorkflowError(f"invalid task_slug: {task_slug!r}")
        async with self._pool.acquire() as conn:
            row = await self._load_task_row(conn, task_slug)
            if row is None:
                raise WorkflowError(f"Task {task_slug!r} does not exist.")
            phases = await self._load_phases(conn, row["id"])
            worktrees = await self._load_worktrees(conn, row["id"])
        extra = row.get("metadata_extra") or {}
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except Exception:
                extra = {}
        return {
            "slug": row["slug"],
            "title": row["title"],
            "workflow": row["workflow"],
            "status": row["status"],
            "current_step": row["current_step"],
            "current_agent": row["current_agent"],
            "complexity": row["complexity"],
            "baseline": extra.get("baseline") or {},
            "worktrees": worktrees,
            "origin": {
                "stream": row["origin_stream"],
                "topic": row["origin_topic"],
            },
            "phases_done": [
                {"step": p["step"], "agent": p["agent"], "artifact": p["artifact"]}
                for p in phases if p["completed_at"] is not None
            ],
            "blocked_reason": row["blocked_reason"],
        }

    async def reopen_task(
        self,
        *,
        task_slug: str,
        agent_name: str,
        next_step: str,
        next_agent: str | None = None,
        reason: str,
        standalone: bool = False,
    ) -> dict[str, Any]:
        """D-57 phase 2.5: reopens a task in a terminal status to run one more step.

        Expected use: the human asks for a revision after closing ("I saw the task
        is done, X is missing"). The orchestrating agent (or another agent explicitly
        instructed) calls reopen_task to move the task back to in_progress at the
        desired step. The reactor receives a normal phase_complete event and
        dispatches to next_agent — same flow as complete_phase.

        Differs from complete_phase in two ways:
          - Does not require an artifact (no phase was completed now; we are only
            restarting the task).
          - Allows transitioning *to* a workflow step without going through the
            previous step's complete_phase (because the terminal state was
            already recorded at the original close).

        Does not change any agent's prompt — by design (see D-57). It stays as an
        available tool, only invoked when the human explicitly asks.
        """
        if not SLUG_RE.match(task_slug):
            raise WorkflowError(f"invalid task_slug: {task_slug!r}")
        if not next_step:
            raise WorkflowError("next_step is required")
        if not reason or not reason.strip():
            raise WorkflowError("reason is required — recorded in blocked_reason for auditing")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                task_row = await self._load_task_row(conn, task_slug)
                if task_row is None:
                    raise WorkflowError(f"Task {task_slug!r} does not exist.")
                current_status = task_row["status"]
                if current_status not in ("done", "blocked", "human_review"):
                    raise WorkflowError(
                        f"Task {task_slug!r} is not in a terminal status (current: {current_status!r}). "
                        f"reopen_task only makes sense to reopen a closed task. For normal progress use complete_phase."
                    )

                wf = self._resolve_workflow(task_row.get("workflow"))
                if wf is None:
                    raise WorkflowError(
                        f"Task {task_slug!r} has no declared workflow — cannot validate next_step."
                    )
                if next_step in TERMINALS:
                    raise WorkflowError(
                        f"next_step cannot be terminal ({next_step}). reopen_task reopens the task to a working step."
                    )
                step_def = wf.step(next_step)
                if step_def is None:
                    raise WorkflowError(
                        f"step {next_step!r} does not exist in workflow {wf.name!r}. "
                        f"Valid steps: {sorted(wf.steps.keys())}."
                    )
                resolved_agent = next_agent or step_def.agent
                if not resolved_agent:
                    raise WorkflowError(
                        f"step {next_step!r} has no default agent; pass an explicit next_agent."
                    )

                now = datetime.now(tz=timezone.utc)
                task_id = task_row["id"]

                # Restart the task: status goes back to in_progress, current_step/agent
                # point to the target, blocked_reason kept historically in the
                # event payload (but removed from the row — it is no longer
                # blocked).
                await conn.execute(
                    """UPDATE tasks.tasks
                          SET status = 'in_progress',
                              current_step = $2,
                              current_agent = $3,
                              blocked_reason = NULL,
                              updated_at = now()
                        WHERE id = $1""",
                    task_id, next_step, resolved_agent,
                )

                # Create the in-flight phase for next_step (same as complete_phase does
                # in the non-terminal branch).
                last_idx_row = await conn.fetchval(
                    "SELECT COALESCE(MAX(idx), -1) FROM tasks.phases WHERE task_id = $1",
                    task_id,
                )
                # COALESCE returns -1 for an empty task; +1 gives 0. Do not use
                # `int(x or -1)` — `0 or -1 == -1` in Python breaks the
                # MAX(idx)=0 case (task with exactly one phase) and produces a duplicate.
                next_idx = int(last_idx_row) + 1
                await conn.execute(
                    """INSERT INTO tasks.phases
                        (task_id, idx, step, agent, started_at)
                       VALUES ($1, $2, $3, $4, $5)""",
                    task_id, next_idx, next_step, resolved_agent, now,
                )

                # phase_complete event for the reactor to dispatch. from_step points
                # to the last step completed before the terminal — informational
                # only, does not drive validation.
                last_done = await conn.fetchrow(
                    """SELECT step FROM tasks.phases
                        WHERE task_id = $1 AND completed_at IS NOT NULL
                        ORDER BY idx DESC LIMIT 1""",
                    task_id,
                )
                from_step = last_done["step"] if last_done else (task_row.get("current_step") or "?")

                next_artifact_default = self._expected_artifact_for(wf, next_step)
                resolved_origin_stream = task_row.get("origin_stream")
                resolved_origin_topic = task_row.get("origin_topic")
                resolved_next_topic = self._resolve_handoff_topic(
                    explicit_topic=None,
                    next_agent=resolved_agent,
                    task_slug=task_slug,
                    origin_stream=resolved_origin_stream,
                    origin_topic=resolved_origin_topic,
                    standalone=bool(standalone),
                )
                next_step_def = wf.step(next_step) if wf else None
                payload = {
                    "task_slug": task_slug,
                    "from_step": from_step,
                    "from_agent": agent_name,
                    "artifact": f"reopen:{reason[:100]}",
                    "summary": reason,
                    "next": next_step,
                    "next_agent": resolved_agent,
                    "next_topic": resolved_next_topic,
                    "next_artifact": next_artifact_default,
                    "next_fresh_session": (
                        next_step_def.fresh_session if next_step_def else False
                    ),
                    "origin_stream": resolved_origin_stream,
                    "origin_topic": resolved_origin_topic,
                    "workflow": task_row.get("workflow"),
                    "reopen": True,
                    "prev_status": current_status,
                    "standalone": bool(standalone),
                }
                event_id = await conn.fetchval(
                    """INSERT INTO orchestrator.events (emitted_by, event_type, task_slug, payload)
                       VALUES ($1, 'phase_complete', $2, $3::jsonb)
                       RETURNING id""",
                    agent_name, task_slug, json.dumps(payload),
                )

        log.info(
            "workflow.reopen_task",
            task_slug=task_slug,
            prev_status=current_status,
            next_step=next_step,
            next_agent=resolved_agent,
            reason=reason[:100],
            event_id=event_id,
        )

        return {
            "event_id": event_id,
            "task_slug": task_slug,
            "prev_status": current_status,
            "next_step": next_step,
            "next_agent": resolved_agent,
            "next_artifact": next_artifact_default,
            "status": "in_progress",
            "guidance": (
                f"Task {task_slug!r} reopened — status is back to 'in_progress' at step '{next_step}', "
                f"the reactor will post the request at #{resolved_agent}/{resolved_next_topic}. "
                f"Reason recorded in the event: {reason[:200]}"
            ),
        }
