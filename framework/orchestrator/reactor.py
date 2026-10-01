"""Orchestrator reactor — consumes orchestrator.events and posts handoffs via the broker.

- LISTEN event_new on Postgres (a trigger fires on each pending INSERT).
- For each pending event:
    next in {done, halt, human_review}: posts to #TERMINAL_NOTIFY_STREAM
    next = agent name: posts a handoff to #<agent> on the given topic
- Marks status processed (or quarantined + attempts+=1 on error).
- Polling fallback every POLL_INTERVAL_SEC (in case LISTEN drops).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import time

import aiohttp
import asyncpg
import structlog

from .health import start_health_server


DATABASE_URL = os.environ["DATABASE_URL"]
BROKER_URL = os.environ["BROKER_URL"].rstrip("/")
BROKER_TOKEN = os.environ["BROKER_TOKEN"]
POLL_INTERVAL_SEC = float(os.environ.get("POLL_INTERVAL_SEC", "2.0"))
QUARANTINE_MAX_RETRIES = int(os.environ.get("QUARANTINE_MAX_RETRIES", "3"))
# Stream that aggregates terminals. If empty, the reactor posts only to the origin
# conversation. Instances set the stream name via env (e.g. 'orchestration',
# 'ops', 'tasks-feed', etc).
TERMINAL_NOTIFY_STREAM = os.environ.get("TERMINAL_NOTIFY_STREAM", "").strip()
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "8810"))

# health stats
_stats = {"started_at": time.time(), "events_processed": 0, "last_event_at": None}

logging_level = os.environ.get("LOG_LEVEL", "INFO").upper()
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, logging_level, 20)),
    logger_factory=structlog.PrintLoggerFactory(),
)
structlog.contextvars.bind_contextvars(component="orchestrator-reactor")
log = structlog.get_logger("reactor")


async def post_message(
    http: aiohttp.ClientSession,
    stream: str,
    topic: str,
    content: str,
    parent_conv_id: int | None = None,
) -> None:
    """D-87: `parent_conv_id` is persisted in messaging.conversations when
    the conv is created (first msg). complete_phase handoffs pass the
    origin conv_id as parent — hierarchy via FK, no heuristics."""
    body: dict = {"stream": stream, "topic": topic, "content": content}
    if parent_conv_id is not None:
        body["parent_conv_id"] = parent_conv_id
    async with http.post(
        f"{BROKER_URL}/api/messages",
        json=body,
    ) as r:
        if r.status >= 400:
            body_err = await r.text()
            raise RuntimeError(f"post failed HTTP {r.status}: {body_err[:200]}")


def _handoff_body(ev_payload: dict) -> str:
    slug = ev_payload.get("task_slug")
    next_artifact = ev_payload.get("next_artifact")
    # Backlog promotion: payload carries `backlog_slug` + `backlog_content`.
    # Different format — the human/agent needs to see the item body as the
    # first message, otherwise the backlog's original context is invisible.
    # We don't reference `company/tasks/<slug>/` because the directory doesn't
    # exist on the FS yet at promotion time (there's only a DB row).
    if ev_payload.get("backlog_slug"):
        title = ev_payload.get("summary", "—").replace("promoted from backlog: ", "", 1)
        body = (ev_payload.get("backlog_content") or "").strip()
        lines = [
            f"📋 **Promoted from backlog** — `{ev_payload.get('backlog_slug')}`",
            "",
            f"**Task:** `{slug}` — {title}",
            f"**Promoted by:** `{ev_payload.get('from_agent')}`",
            "",
            "---",
            "",
            body if body else "_(backlog item with no content)_",
            "",
            "---",
            "",
            f"_When done, call `complete_phase(task_slug='{slug}', "
            f"artifact='{next_artifact or '<your-artifact>'}', summary=..., next=...)`._",
        ]
        return "\n".join(lines)
    # Normal handoff (phase -> phase).
    from_step = ev_payload.get("from_step") or ev_payload.get("from_phase")
    next_step = ev_payload.get("next")
    lines = [
        f"➡️ **Handoff from `{ev_payload.get('from_agent')}`**",
        "",
        f"**Task:** `{slug}` — you take over phase **{next_step}**.",
        f"**Previous step:** `{from_step}` (artifact: `{ev_payload.get('artifact')}`)",
        "",
        f"**Summary:** {ev_payload.get('summary', '—')}",
        "",
        f"_Context in `company/tasks/{slug}/`. Use `get_task_state(task_slug='{slug}')` to read structured state. "
        f"When done, call `complete_phase(task_slug='{slug}', artifact='{next_artifact or '<your-artifact>'}', summary=..., next=...)`._",
    ]
    return "\n".join(lines)


def _terminal_body(ev_payload: dict) -> str:
    slug = ev_payload.get("task_slug")
    next_ = ev_payload.get("next")
    flag = {
        "done": "🏁",
        "halt": "⏸️",
        "human_review": "🙋",
    }.get(next_, "❓")
    label = {
        "done": "closed successfully",
        "halt": "paused — human needs to unblock",
        "human_review": "escalated for human review",
    }.get(next_, next_ or "—")
    return (
        f"{flag} **Task `{slug}` {label}**\n\n"
        f"**Summary:** {ev_payload.get('summary', '—')}\n"
        f"**Origin:** `{ev_payload.get('from_agent')}` / step `{ev_payload.get('from_step')}` / "
        f"artifact `{ev_payload.get('artifact')}`"
    )


async def _resolve_conv_id(
    pool: asyncpg.Pool, stream: str | None, topic: str | None
) -> int | None:
    """D-87: resolves conversation_id via (stream, topic). Used to set
    parent_conv_id on the child conv created by a complete_phase handoff/terminal."""
    if not stream or not topic:
        return None
    row = await pool.fetchrow(
        """SELECT c.id
             FROM messaging.conversations c
             JOIN messaging.streams s ON s.id = c.stream_id
            WHERE s.name = $1 AND c.topic_name = $2
            LIMIT 1""",
        stream, topic,
    )
    return row["id"] if row else None


async def _process_one(http: aiohttp.ClientSession, pool: asyncpg.Pool, event_id: int) -> None:
    """Marks processing, posts, marks processed (or quarantined)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """UPDATE orchestrator.events SET status = 'processing'
                   WHERE id = $1 AND status = 'pending' RETURNING id, payload, attempts""",
                event_id,
            )
            if row is None:
                return  # already processed by another worker
    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    try:
        next_ = payload.get("next")
        task_slug = payload.get("task_slug")
        # Post-terminal guard: ignore a stale handoff if the task is already in
        # a terminal status (`done`/`blocked`/`human_review`). Can happen
        # when a child `complete_phase` arrives late (e.g. after the PO has
        # closed the task on a parallel path). Terminal events themselves
        # (next in done/halt/human_review) skip this guard — they
        # are **declaring** the terminal, not reacting to one.
        if task_slug and next_ not in ("done", "halt", "human_review"):
            t_status = await pool.fetchval(
                "SELECT status FROM tasks.tasks WHERE slug = $1",
                task_slug,
            )
            if t_status in ("done", "blocked", "human_review"):
                # Mark the event processed to remove it from the queue without
                # posting anything. last_error records the reason for auditing.
                await pool.execute(
                    """UPDATE orchestrator.events
                          SET status = 'processed', processed_at = now(),
                              last_error = $2
                        WHERE id = $1""",
                    event_id, f"dropped: task already in terminal status {t_status!r}",
                )
                log.info(
                    "reactor.event_dropped_post_terminal",
                    event_id=event_id, task_slug=task_slug,
                    task_status=t_status, payload_next=next_,
                )
                return
        if next_ in ("done", "halt", "human_review"):
            # Terminal: status already persisted by the WorkflowManager; here we
            # only notify. Two destinations, both best-effort (a post failure
            # logs a warning, doesn't fail the event):
            #   (1) TERMINAL_NOTIFY_STREAM (configurable via env) — feed
            #       aggregating all of the company's terminals.
            #   (2) origin conversation (origin_stream/origin_topic in
            #       metadata) — where the human asked for the task; that's where
            #       they'll notice it finished without switching tabs.
            terminal_body = _terminal_body(payload)
            origin_stream = payload.get("origin_stream")
            origin_topic = payload.get("origin_topic")
            # D-87: origin is the conv that started the task — it becomes the parent of
            # both (aggregator terminal + reply in origin) for the visual cascade.
            origin_conv_id = await _resolve_conv_id(pool, origin_stream, origin_topic)
            if TERMINAL_NOTIFY_STREAM:
                try:
                    await post_message(
                        http, TERMINAL_NOTIFY_STREAM, f"task-{task_slug}-final",
                        terminal_body,
                        parent_conv_id=origin_conv_id,
                    )
                except Exception as post_exc:
                    log.warning(
                        "reactor.terminal_notify_failed",
                        event_id=event_id, task_slug=task_slug, next=next_,
                        stream=TERMINAL_NOTIFY_STREAM,
                        error=str(post_exc)[:300],
                    )
            if origin_stream and origin_topic:
                # Skip echoing to the agent itself: if whoever completed the final phase
                # owns origin_stream, posting terminal_body to the same
                # conv wakes the agent up again (the message lands in its
                # inbox), causing a cascade of redundant runs after
                # `done`. Symptom seen in a pilot on 2026-05-05:
                # 3 runs after `done` costing ~$0.20 / 7 turns per task,
                # producing msgs like "Task closed", "Late message from the
                # reactor", etc. In the classic multi-agent setup this didn't show up
                # because executor (from_agent) and PO (origin_stream) are
                # different — the post in origin woke the PO, which was the
                # desired behavior. We keep that behavior here:
                # we only skip when from_agent == origin_stream. The
                # TERMINAL_NOTIFY_STREAM post (above) still happens
                # — aggregator feed for the human.
                from_agent = payload.get("from_agent")
                if from_agent and from_agent == origin_stream:
                    log.info(
                        "reactor.terminal_origin_skipped_self",
                        event_id=event_id, task_slug=task_slug,
                        from_agent=from_agent, origin_stream=origin_stream,
                    )
                else:
                    try:
                        # Same origin_stream+origin_topic — the conv already exists, so
                        # parent_conv_id is ignored by the broker (ON CONFLICT path).
                        await post_message(http, origin_stream, origin_topic, terminal_body)
                    except Exception as post_exc:
                        log.warning(
                            "reactor.origin_notify_failed",
                            event_id=event_id, task_slug=task_slug,
                            stream=origin_stream, topic=origin_topic,
                            error=str(post_exc)[:300],
                        )
        else:
            target_stream = payload.get("next_agent") or next_  # back-compat: old events had the agent in 'next'
            target_topic = payload.get("next_topic") or f"task-{task_slug}"
            origin_stream = payload.get("origin_stream")
            origin_topic = payload.get("origin_topic")
            # fresh_session: the workflow declared that entering this step needs a
            # fresh context window. Clear the conv's claude_session_id before
            # the post — the next claude_runner spawn doesn't use --resume and runs
            # `claude -p` with no prior buffer. The step's `instructions` still
            # come in via append-system-prompt (built dynamically in claude_runner).
            # No-op if the conv doesn't exist yet (UPDATE 0 rows).
            if payload.get("next_fresh_session"):
                cleared = await pool.execute(
                    """UPDATE messaging.conversations c
                          SET claude_session_id = NULL,
                              claude_session_cwd = NULL
                         FROM messaging.streams s
                        WHERE c.stream_id = s.id
                          AND s.name = $1
                          AND c.topic_name = $2""",
                    target_stream, target_topic,
                )
                log.info(
                    "reactor.fresh_session_cleared",
                    event_id=event_id, task_slug=task_slug,
                    target_stream=target_stream, target_topic=target_topic,
                    rows=cleared,
                )
            # ALWAYS post the handoff — even when next_agent == from_agent
            # on the same topic. Trying to skip it as a "redundant self-handoff" stalls
            # the phase: the agent that called complete_phase ends its turn right
            # after (run_end), so the next phase is orphaned with nobody
            # waking up to run it. The design assumed "the agent continues in the same
            # turn", but claude_runner has no such guarantee — after
            # complete_phase, the agent may stop (it almost always does).
            # Symptom confirmed on 2026-04-30 in a task: the PO called
            # complete_phase(next=<closing step>) in the conv-task, run_end right
            # after, the reactor skipped the handoff, the closing phase never
            # ran — task stuck with current_step at the closing step.
            #
            # The original symptom behind the skip ("this handoff is a
            # duplicate event", D-103/cb009d3) was the PO calling
            # complete_phase in a human-PO conv (free chat, not a conv-task);
            # the posted handoff became visual noise there. Today, after the prompt
            # refactor, the PO works in a conv-task (`product-owner/task-X`),
            # and the handoff posted in that conv is exactly the trigger for the phase
            # to run. If a future case repeats D-103 (PO in a human conv
            # calling complete_phase), the worst case is the handoff
            # becoming visual noise there — functionally nothing stalls.
            origin_conv_id = await _resolve_conv_id(
                pool, origin_stream, origin_topic,
            )
            # Path A of `backlog_promote` (D-110): the orchestrator declared
            # by the workflow != dispatch_agent. Creates a supervisor conv in the
            # orchestrator's stream before the handoff, with a pointer message so the
            # human can find where to track the task. The initial phase's
            # conv (target) becomes a child of the supervisor — terminal_notify
            # goes back to the supervisor when the task finishes.
            #
            # In Path B (orchestrator == dispatch_agent), the "supervisor"
            # is the first phase's own conv (same stream + same topic);
            # the post_message right below creates that unified conv — no
            # separate supervisor, no extra message.
            is_promote = bool(payload.get("backlog_slug"))
            if (is_promote
                    and origin_conv_id is None
                    and origin_stream
                    and origin_topic
                    and (origin_stream, origin_topic) != (target_stream, target_topic)):
                supervisor_body = (
                    f"📋 **Task promoted** — `{task_slug}`\n\n"
                    f"Orchestrator: `{origin_stream}` (you).\n"
                    f"Initial phase dispatched to `{target_stream}`.\n\n"
                    f"Track from here — terminals (`done`, `halt`, `human_review`) "
                    f"come back to this conv when the task closes."
                )
                await post_message(
                    http, origin_stream, origin_topic, supervisor_body,
                    parent_conv_id=None,  # supervisor is root
                )
                # re-resolve now that the conv exists
                origin_conv_id = await _resolve_conv_id(
                    pool, origin_stream, origin_topic,
                )
            # standalone=True (mega-agent fan-out): don't link parent_conv_id,
            # the conv-task stays root in the target stream. It shows in the sidebar and
            # the human can chat in it (D-96 hides children + read-only). Without
            # standalone, the default behavior keeps the hierarchy (multi-agent
            # via PO/executor: the human only talks to the PO; tasks become read-only
            # children in the header). The logical task<->origin link remains via
            # tasks.tasks.origin_stream/origin_topic — telemetry and the task
            # page UI keep traceability.
            if payload.get("standalone"):
                log.info(
                    "reactor.standalone_handoff",
                    event_id=event_id, task_slug=task_slug,
                    target_stream=target_stream, target_topic=target_topic,
                )
                effective_parent = None
            else:
                effective_parent = origin_conv_id
            await post_message(
                http, target_stream, target_topic, _handoff_body(payload),
                parent_conv_id=effective_parent,
            )
        await pool.execute(
            "UPDATE orchestrator.events SET status = 'processed', processed_at = now() WHERE id = $1",
            event_id,
        )
        log.info("reactor.processed", event_id=event_id, next=next_, task_slug=task_slug)
        _stats["events_processed"] += 1
        _stats["last_event_at"] = time.time()
    except Exception as e:
        new_attempts = row["attempts"] + 1
        new_status = "pending" if new_attempts < QUARANTINE_MAX_RETRIES else "dead"
        await pool.execute(
            "UPDATE orchestrator.events SET status = $2, attempts = $3, last_error = $4 WHERE id = $1",
            event_id, new_status, new_attempts, str(e)[:500],
        )
        log.exception("reactor.process_failed", event_id=event_id, attempt=new_attempts, status=new_status)


async def _drain_pending(http: aiohttp.ClientSession, pool: asyncpg.Pool) -> None:
    """Fetches pending events (FIFO order) and processes them one by one."""
    rows = await pool.fetch(
        """SELECT id FROM orchestrator.events
             WHERE status = 'pending' AND attempts < $1
             ORDER BY created_at ASC LIMIT 50""",
        QUARANTINE_MAX_RETRIES,
    )
    for r in rows:
        await _process_one(http, pool, r["id"])


def _health_status() -> dict:
    return {
        "status": "ok",
        "uptime_sec": int(time.time() - _stats["started_at"]),
        "events_processed": _stats["events_processed"],
        "last_event_at": _stats["last_event_at"],
    }


async def main():
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4)
    start_health_server(HEALTH_PORT, _health_status)
    log.info("reactor.starting", broker=BROKER_URL, terminal_stream=TERMINAL_NOTIFY_STREAM, health_port=HEALTH_PORT)

    async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {BROKER_TOKEN}"}) as http:
        # Initial drain
        await _drain_pending(http, pool)

        # LISTEN on a dedicated conn
        listen_conn = await asyncpg.connect(DATABASE_URL)
        notify_q: asyncio.Queue = asyncio.Queue()

        def _cb(_conn, _pid, _channel, payload):
            notify_q.put_nowait(payload)

        await listen_conn.add_listener("event_new", _cb)

        while True:
            try:
                await asyncio.wait_for(notify_q.get(), timeout=POLL_INTERVAL_SEC)
            except asyncio.TimeoutError:
                pass
            await _drain_pending(http, pool)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
