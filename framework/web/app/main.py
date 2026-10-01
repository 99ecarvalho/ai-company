"""FastAPI app — serves the PWA + internal broker + transcribe/push/memory/telemetry/hire endpoints.

Messaging/conversations/streams/users/asks/events routes live in broker.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import asyncpg
import bcrypt
import docker
import structlog
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import auth as auth_mod
from . import db
from .auth import (
    Principal,
    SESSION_COOKIE,
    SESSION_TTL_DAYS,
    cookie_secure,
    create_session,
    delete_session,
    get_principal,
)
from .broker import router as broker_router
from .files import router as files_router
from .scheduler_routes import router as scheduler_routes_router
from .push_dispatcher import PushDispatcher


# ---------- Logging ----------

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    logger_factory=structlog.PrintLoggerFactory(),
)
structlog.contextvars.bind_contextvars(component="web")
log = structlog.get_logger("web.main")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
BUILD_DIR = Path(__file__).resolve().parent.parent / "build"
# D-94: instance/web/ lets each instance override PWA assets (icons, later
# custom CSS) without editing tracked code. Bind-mounted at /workspace/web/
# by compose. The override wins over the framework default when an
# equivalent file exists.
INSTANCE_WEB_DIR = Path(os.environ.get("INSTANCE_WEB_DIR", "/workspace/web"))


# ---------- Lifespan ----------

async def _bootstrap_admin_user():
    """Ensures the human admin exists in messaging.users.

    Idempotent: ON CONFLICT updates is_admin/full_name. When
    ADMIN_PASSWORD is set in the env, it also updates password_hash
    (allows rotating the password via .env + restart, without touching
    the database). When empty, it keeps the existing hash — so instances
    that already had a valid password are not wiped if a restart comes
    without the env.
    """
    email = os.environ.get("ADMIN_EMAIL", "admin@example.com")
    pwd = os.environ.get("ADMIN_PASSWORD", "")
    pwd_hash = bcrypt.hashpw(pwd.encode(), bcrypt.gensalt()).decode() if pwd else None
    await db.execute(
        """
        INSERT INTO messaging.users (email, username, full_name, kind, password_hash, is_admin)
        VALUES ($1, $2, $3, 'human', $4, true)
        ON CONFLICT (email) DO UPDATE SET
            is_admin = true,
            full_name = COALESCE(EXCLUDED.full_name, messaging.users.full_name),
            password_hash = COALESCE(EXCLUDED.password_hash, messaging.users.password_hash)
        """,
        email, email.split("@")[0], "Admin", pwd_hash,
    )
    log.info("web.admin_bootstrapped", email=email, password_set=bool(pwd_hash))


async def _bootstrap_system_bot():
    """Dedicated user for service tokens (reactor/scheduler) to post messages.
    agent_name=NULL — does not show up in /api/agents (PWA sidebar).
    """
    await db.execute(
        """
        INSERT INTO messaging.users (email, username, full_name, kind, agent_name, is_admin)
        VALUES ('system-bot@internal.ai-company', 'system-bot', 'System', 'bot', NULL, true)
        ON CONFLICT (email) DO UPDATE SET
            is_admin = true,
            agent_name = NULL
        """,
    )
    log.info("web.system_bot_bootstrapped")


async def _bootstrap_terminal_stream():
    """Ensures the terminal aggregator stream (task done/halt/human_review)
    exists, if configured via env. Without it, the reactor tries to post to a
    nonexistent stream. Idempotent. No-op when TERMINAL_NOTIFY_STREAM is empty.
    """
    terminal = os.environ.get("TERMINAL_NOTIFY_STREAM", "").strip()
    if not terminal:
        log.info("web.terminal_stream_skipped", reason="TERMINAL_NOTIFY_STREAM empty")
        return
    await db.execute(
        """
        INSERT INTO messaging.streams (name, description)
        VALUES ($1, 'Task terminal notifications (done/halt/human_review)')
        ON CONFLICT (name) DO NOTHING
        """,
        terminal,
    )
    log.info("web.terminal_stream_bootstrapped", stream=terminal)


async def _reload_push_dispatcher() -> bool:
    """(Re)creates app.state.push_dispatcher from the current config
    (web.app_settings + env fallback). Returns True if it ended up enabled."""
    from . import app_settings as _s
    cfg = await _s.get_vapid()
    if not cfg:
        app.state.push_dispatcher = None
        return False
    app.state.push_dispatcher = PushDispatcher(
        vapid_private_key=cfg["private_key"],
        vapid_claims_sub=cfg.get("contact_email") or "admin@example.com",
    )
    return True


async def _push_notifier_loop():
    """LISTENs on msg_all and fires a VAPID push ONLY when there is an
    unresolved pending_ask in the conversation (i.e. an agent is blocked
    waiting for the human's answer). A normal bot reply does not push — it
    shows up as a discreet unread in the sidebar until the human opens it.

    Rationale: a push is an interruption — reserved for real demands on
    attention (ask_human), not for every bot reply/signal emoji.

    Hot-reload: the dispatcher is read fresh on every msg instead of cached,
    so clicking Generate keypair in the PWA starts firing pushes without a
    restart. If there is no dispatcher yet, the msg is ignored (no panic loop).
    """
    dsn = os.environ["DATABASE_URL"]
    while True:
        try:
            conn = await asyncpg.connect(dsn)
            queue: asyncio.Queue[str] = asyncio.Queue()

            def _cb(_conn, _pid, _channel, payload):
                queue.put_nowait(payload)

            await conn.add_listener("msg_all", _cb)
            log.info("web.push_notifier_started")
            while True:
                try:
                    payload_raw = await asyncio.wait_for(queue.get(), timeout=60)
                except asyncio.TimeoutError:
                    continue
                try:
                    data = json.loads(payload_raw)
                    # Migration 026: echoes (D-100 forward) are purely
                    # visual — skip the push so the human is not notified of a
                    # copy (the original msg in the child conv already had its
                    # own pending_ask evaluation).
                    if data.get("kind") == "echo":
                        continue
                    meta = await db.fetch_one(
                        """SELECT u.username, u.kind AS sender_kind, s.name AS stream, c.topic_name AS topic,
                                  m.content
                             FROM messaging.users u
                             JOIN messaging.messages m ON m.sender_id = u.id
                             JOIN messaging.conversations c ON c.id = m.conversation_id
                             JOIN messaging.streams s ON s.id = c.stream_id
                            WHERE m.id = $1""",
                        data["id"],
                    )
                    if meta is None or meta["sender_kind"] != "bot":
                        continue
                    # D-111: filter kind='ask_human' — ask_agent is
                    # silent for the human (the target agent answers).
                    has_pending = await db.fetch_one(
                        "SELECT 1 FROM messaging.pending_asks "
                        " WHERE conversation_id = $1 AND resolved_at IS NULL "
                        "   AND kind = 'ask_human'",
                        data["conversation_id"],
                    )
                    if not has_pending:
                        continue
                    # Hot-reload: take the current dispatcher. If Generate keypair
                    # ran in the PWA, the new dispatcher is already active here.
                    dispatcher = app.state.push_dispatcher
                    if dispatcher is None:
                        continue
                    content = (meta["content"] or "").strip()
                    first_line = content.splitlines()[0] if content else ""
                    title = f"{meta['username']} needs you"
                    body = f"#{meta['stream']} / {meta['topic']}\n{first_line[:160]}"
                    tag = f"{meta['stream']}/{meta['topic']}"
                    await dispatcher.broadcast(title=title, body=body, url="/", tag=tag)
                except Exception:
                    log.exception("web.push_notify_failed")
        except Exception:
            log.exception("web.push_notifier_crashed")
            await asyncio.sleep(5)
        finally:
            try:
                await conn.close()
            except Exception:
                pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_pool()
    auth_mod.init_service_tokens()
    await _bootstrap_admin_user()
    await _bootstrap_system_bot()
    await _bootstrap_terminal_stream()

    app.state.push_dispatcher = None
    await _reload_push_dispatcher()

    app.state.push_notifier_task = asyncio.create_task(_push_notifier_loop())

    log.info(
        "web.started",
        admin=os.environ.get("ADMIN_EMAIL"),
        vapid_enabled=app.state.push_dispatcher is not None,
        transcriber=os.environ.get("TRANSCRIBER_URL", "(disabled)"),
    )
    yield

    app.state.push_notifier_task.cancel()
    await db.close_pool()
    log.info("web.shutdown")


app = FastAPI(title="ai-company web", version="0.2.0", lifespan=lifespan)
app.include_router(broker_router)
app.include_router(files_router)
app.include_router(scheduler_routes_router)


# ---------- Compat aliases for the legacy PWA ----------
# The PWA expects the {items: [...]} format and ids as "stream/topic" (string).
# The broker returns a plain list with numeric ids. We adapt here.

def _conv_id_string(stream: str, topic: str) -> str:
    return f"{stream}/{topic}"


@app.get("/api/pending-asks")
async def pending_asks_alias(principal: Principal = Depends(get_principal)):
    from .broker import list_asks
    items = await list_asks(principal)
    # PWA expects id=stream/topic
    for it in items:
        it["id"] = _conv_id_string(it["stream"], it["topic"])
    return {"items": items}


@app.post("/api/post-message")
async def post_message_alias(payload: dict, principal: Principal = Depends(get_principal)):
    from .broker import MessageIn, post_message
    msg = MessageIn(
        stream=payload.get("stream"),
        topic=payload.get("topic") or "",
        content=payload.get("content", ""),
        client_id=payload.get("client_id"),
    )
    # If topic came empty/None, create a default with a human-readable timestamp
    # (no prefix — input may come from voice or text, a prefix adds nothing).
    if not msg.topic:
        from datetime import datetime
        msg.topic = datetime.utcnow().strftime('%Y-%m-%d %H:%M')
    result = await post_message(msg, principal)
    return {
        "id": result.id, "stream": result.stream, "topic": result.topic,
        "conversation_id": result.conversation_id, "content": result.content,
        "sent_at": result.sent_at,
    }


@app.get("/api/conversations")
async def conversations_alias(
    filter: str = "active",
    principal: Principal = Depends(get_principal),
):
    """Unified list (D-57): returns Active (archived_at IS NULL) or Closed
    (archived_at IS NOT NULL). Task metadata comes along when the conversation
    is the origin of a task.

    Fields: `archived_at`, `participating`, `task: {...}` when applicable."""
    from .broker import list_unified_conversations
    items = await list_unified_conversations(principal, filter_=filter)
    from datetime import datetime
    def _epoch(dt_iso: str) -> int:
        return int(datetime.fromisoformat(dt_iso.replace("Z", "+00:00")).timestamp())
    out = []
    for c in items:
        last_is_bot = (c["last_sender"] or "").endswith("-bot")
        ts = _epoch(c["last_message_at"])
        entry = {
            "id": _conv_id_string(c["stream"], c["topic"]),
            "db_id": c["id"],
            "agent": c["stream"],
            "stream": c["stream"],
            "topic": c["topic"],
            "custom_title": c.get("custom_title"),
            "last_activity": ts,
            "last_message_at": c["last_message_at"],
            "archived_at": c.get("archived_at"),
            "msg_count": c["msg_count"],
            "last_msg": {
                "sender": c["last_sender"],
                "is_bot": last_is_bot,
                "content_preview": (c["last_content"] or "")[:200],
                "content": c["last_content"] or "",
                "timestamp": ts,
            },
            "awaiting_human": c["awaiting_human"],
            "participating": c["participating"],
            # Legacy `closed` (web.closed_conversations, pre-D-57) no longer
            # applies — the filter is archived_at. Kept as False for the legacy shape.
            "closed": False,
            "parent_conv_id": c.get("parent_conv_id"),
            "children_stats": c.get("children_stats") or {
                "active": 0, "stuck": 0, "resolved": 0,
            },
            "is_running": bool(c.get("is_running")),
            "is_stuck": bool(c.get("is_stuck")),
            "is_errored": bool(c.get("is_errored")),
            "task_not_current_agent": bool(c.get("task_not_current_agent")),
        }
        if c.get("task"):
            entry["task"] = c["task"]
        out.append(entry)
    return {"items": out}


async def _resolve_conv_id(conv_id: str) -> int:
    """Accepts 'stream/topic' or a number (string). Returns the numeric id."""
    if conv_id.isdigit():
        return int(conv_id)
    if "/" not in conv_id:
        raise HTTPException(status_code=400, detail="invalid conv_id (use stream/topic)")
    stream, topic = conv_id.split("/", 1)
    row = await db.fetch_one(
        """SELECT c.id FROM messaging.conversations c
             JOIN messaging.streams s ON s.id = c.stream_id
            WHERE s.name = $1 AND c.topic_name = $2""",
        stream, topic,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="conversation does not exist")
    return row["id"]


@app.get("/api/conversations/{conv_id:path}/messages")
async def conversation_messages_alias(conv_id: str, principal: Principal = Depends(get_principal)):
    numeric_id = await _resolve_conv_id(conv_id)
    rows = await db.fetch_all(
        """SELECT m.id, u.username AS sender, u.kind AS sender_kind,
                  m.content, m.sent_at,
                  s.name AS stream, c.topic_name AS topic, c.custom_title
             FROM messaging.messages m
             JOIN messaging.users u ON u.id = m.sender_id
             JOIN messaging.conversations c ON c.id = m.conversation_id
             JOIN messaging.streams s ON s.id = c.stream_id
            WHERE m.conversation_id = $1
            ORDER BY m.id ASC LIMIT 500""",
        numeric_id,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="conversation has no messages")
    first = rows[0]
    # D-111: kind='ask_human' — the needs-you badge only fires on a human ask,
    # not on ask_agent (the target agent answers, silent for the human).
    pending = await db.fetch_one(
        "SELECT 1 FROM messaging.pending_asks "
        " WHERE conversation_id = $1 AND resolved_at IS NULL "
        "   AND kind = 'ask_human'",
        numeric_id,
    )
    # D-71: runner_state derived from telemetry.live_events + pending_asks.
    # Same shape as the standalone /runner-state endpoint.
    runner_state = await _compute_runner_state(numeric_id)
    # D-96: parent_conv_id alone defines read-only. Every child is read-only
    # for the human (D-96 collapsed the old rule). The frontend derives it
    # directly from parent_conv_id; the `read_only_reason` field was removed.
    parent_conv_id_row = await db.fetch_one(
        "SELECT parent_conv_id FROM messaging.conversations WHERE id = $1",
        numeric_id,
    )
    parent_conv_id = parent_conv_id_row["parent_conv_id"] if parent_conv_id_row else None
    return {
        "id": conv_id,
        "agent": first["stream"],
        "stream": first["stream"],
        "topic": first["topic"],
        "custom_title": first["custom_title"],
        "pending_ask_id": numeric_id if pending else None,
        "runner_state": runner_state,
        "parent_conv_id": parent_conv_id,
        "messages": [
            {
                "id": r["id"],
                "sender": r["sender"],
                "is_bot": r["sender_kind"] == "bot",
                "content": r["content"],
                "timestamp": int(r["sent_at"].timestamp()),
            } for r in rows
        ],
    }


async def _collect_descendant_conv_ids(root_id: int) -> list[int]:
    """Recursively discovers all descendant convs of `root_id`.

    Primary source (D-87): recursive CTE on the `parent_conv_id` FK. O(depth)
    lookup instead of a topic-pattern heuristic. Catches grandchildren and
    great-grandchildren the legacy parser missed (e.g. topic
    `__ask-from-<grandparent>.<parent>-<uid>` did not match the pattern
    `__ask-from-<parent>-%`).

    Legacy fallback: for convs older than D-87 (parent_conv_id NULL),
    keeps the D-86 heuristics:
      1. ask_agent: convs with topic `__ask-from-<cur.stream>-<uid>` whose
         matching tool_use in telemetry.live_events was emitted by the
         current conv.
      2. task: if cur is the origin of a task, all `task-<slug>` convs
         are children.

    Returns ids in discovery order (top-down).
    """
    # Primary source: recursive CTE.
    rows = await db.fetch_all(
        """WITH RECURSIVE tree AS (
               SELECT id FROM messaging.conversations WHERE id = $1
             UNION ALL
               SELECT c.id FROM messaging.conversations c
                 JOIN tree t ON c.parent_conv_id = t.id
           )
           SELECT id FROM tree WHERE id != $1""",
        root_id,
    )
    descendants: list[int] = [r["id"] for r in rows]
    visited: set[int] = {root_id, *descendants}

    # Legacy fallback: apply heuristics only to convs without parent_conv_id
    # (pre-D-87). If all active convs were created post-D-87, the loop
    # below adds nothing and the cost is negligible (one query).
    legacy_roots_rows = await db.fetch_all(
        """SELECT id FROM messaging.conversations
            WHERE parent_conv_id IS NULL
              AND id = ANY($1::int[])""",
        [root_id, *descendants],
    )
    legacy_queue: list[int] = [r["id"] for r in legacy_roots_rows]

    while legacy_queue:
        cur_id = legacy_queue.pop(0)
        cur = await db.fetch_one(
            """SELECT s.name AS stream, c.topic_name AS topic
                 FROM messaging.conversations c
                 JOIN messaging.streams s ON s.id = c.stream_id
                WHERE c.id = $1""",
            cur_id,
        )
        if not cur:
            continue

        # Legacy phase 1: ask_agent children (topic __ask-from-<cur_stream>-*).
        pattern = f"__ask-from-{cur['stream']}-%"
        candidates = await db.fetch_all(
            """SELECT c.id, c.created_at, s.name AS stream
                 FROM messaging.conversations c
                 JOIN messaging.streams s ON s.id = c.stream_id
                WHERE c.topic_name LIKE $1
                  AND c.parent_conv_id IS NULL""",
            pattern,
        )
        for cand in candidates:
            if cand["id"] in visited:
                continue
            tool_hit = await db.fetch_one(
                """SELECT le.id
                     FROM telemetry.live_events le
                    WHERE le.conversation_id = $1
                      AND le.kind = 'tool_use'
                      AND le.data::text LIKE '%ask_agent%'
                      AND (le.data->>'input') LIKE '%' || $2::text || '%'
                      AND le.ts <= ($3::timestamptz + interval '10 seconds')
                    LIMIT 1""",
                cur_id, cand["stream"], cand["created_at"],
            )
            if tool_hit:
                visited.add(cand["id"])
                descendants.append(cand["id"])
                legacy_queue.append(cand["id"])

        # Legacy phase 2: task siblings.
        task_row = await db.fetch_one(
            """SELECT slug FROM tasks.tasks
                WHERE origin_stream = $1 AND origin_topic = $2""",
            cur["stream"], cur["topic"],
        )
        if task_row:
            task_topic = f"task-{task_row['slug']}"
            task_convs = await db.fetch_all(
                """SELECT c.id FROM messaging.conversations c
                    WHERE c.topic_name = $1 AND c.id != $2
                      AND c.parent_conv_id IS NULL""",
                task_topic, cur_id,
            )
            for tc in task_convs:
                if tc["id"] in visited:
                    continue
                visited.add(tc["id"])
                descendants.append(tc["id"])
                legacy_queue.append(tc["id"])

    return descendants


@app.post("/api/conversations/close-all")
async def conversations_close_all_alias(principal: Principal = Depends(get_principal)):
    from .broker import close_all_conversations
    return await close_all_conversations(principal)


async def _cancel_runners_for_convs(conv_ids: list[int], silent: bool = False) -> None:
    """Fires `pg_notify('agent_ctrl', cancel_topic)` for each conversation.
    The agent listening on the matching stream sends SIGTERM to the topic's
    active Claude CLI process (SIGKILL fallback after 3s, via dispatcher).

    Called on cascading archive/delete — without it, active runners in
    removed convs become ghosts holding pool slots, blocking future tasks
    (D-72 bugfix).

    `silent=True` tells the dispatcher NOT to post the confirmation
    message ("cancelled by user" / "nothing to cancel"). Used by the
    archive/delete callers: the conv was already archived/deleted by the
    human, so the notification would be noise in the Closed tab. Manual
    cancel via POST /api/conversations/{id}/cancel keeps `silent=False` —
    the feedback there is useful to confirm the signal was processed.
    """
    if not conv_ids:
        return
    rows = await db.fetch_all(
        """SELECT s.name AS stream, c.topic_name AS topic
             FROM messaging.conversations c
             JOIN messaging.streams s ON s.id = c.stream_id
            WHERE c.id = ANY($1::int[])""",
        conv_ids,
    )
    for r in rows:
        payload = json.dumps({
            "type": "cancel_topic",
            "stream": r["stream"],
            "topic": r["topic"],
            "user_id": None,
            "silent": silent,
        })
        await db.execute("SELECT pg_notify('agent_ctrl', $1)", payload)


@app.patch("/api/conversations/{conv_id:path}")
async def conversation_patch(
    conv_id: str,
    payload: dict,
    _: Principal = Depends(get_principal),
):
    """Updates the conversation's editable metadata. Currently only accepts
    `custom_title` (friendly title set by the human via the PWA — the frontend
    display rule is `custom_title || task.title || topic`). Passing an empty
    string or null clears the override (back to the fallback). Migration 027."""
    if "custom_title" not in payload:
        raise HTTPException(status_code=400, detail="custom_title is required")
    raw = payload.get("custom_title")
    if raw is None:
        new_title: str | None = None
    elif isinstance(raw, str):
        trimmed = raw.strip()
        new_title = trimmed if trimmed else None
    else:
        raise HTTPException(status_code=400, detail="custom_title must be string or null")
    if new_title is not None and len(new_title) > 200:
        raise HTTPException(status_code=400, detail="custom_title exceeds 200 characters")
    numeric_id = await _resolve_conv_id(conv_id)
    row = await db.fetch_one(
        """UPDATE messaging.conversations
              SET custom_title = $2
            WHERE id = $1
        RETURNING id, custom_title""",
        numeric_id, new_title,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="conversation does not exist")
    return {"ok": True, "id": row["id"], "custom_title": row["custom_title"]}


@app.post("/api/conversations/{conv_id:path}/archive")
async def conversation_archive(conv_id: str, principal: Principal = Depends(get_principal)):
    """D-57: manual soft-delete of a thread by the human. Moves it to the Closed
    tab in the PWA. Unlike task archive (which requires a terminal status),
    conversation archive is unrestricted — it is the human's decision, not
    task mechanics.

    D-72: cascade — descendants (sub-conversations via ask_agent + `task-<slug>`
    convs of the same task) are archived along. Active runners of the
    involved convs get a cancel via agent_ctrl so they don't become ghosts
    holding the pool.

    Bot caller (MCP `archive_conversation`): additionally blocks if the bot
    itself has an open `pending_ask` in the conv — archiving would silence
    the human before the answer. A human caller (PWA) has no such rule
    (free decision)."""
    numeric_id = await _resolve_conv_id(conv_id)
    row = await db.fetch_one(
        "SELECT archived_at FROM messaging.conversations WHERE id = $1",
        numeric_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="conversation does not exist")
    if row["archived_at"] is not None:
        raise HTTPException(status_code=409, detail="conversation is already archived")
    if principal.kind == "bot" and principal.user_id is not None:
        # D-111: block archive only if there is an own pending ask_human.
        # A pending ask_agent (cross-agent) must not block archive — the bot
        # archiving means "I abandon the ask".
        own_pending = await db.fetch_one(
            """SELECT 1 FROM messaging.pending_asks
                WHERE conversation_id = $1
                  AND asker_id = $2
                  AND resolved_at IS NULL
                  AND kind = 'ask_human'""",
            numeric_id, principal.user_id,
        )
        if own_pending:
            raise HTTPException(
                status_code=409,
                detail=(
                    "You have an open ask_human in this conversation — archiving "
                    "would silence the human before they answer. Wait for the "
                    "answer or resolve the ask before calling archive."
                ),
            )
    descendants = await _collect_descendant_conv_ids(numeric_id)
    all_ids = [numeric_id, *descendants]
    await _cancel_runners_for_convs(all_ids, silent=True)
    result = await db.execute(
        """UPDATE messaging.conversations
              SET archived_at = now()
            WHERE id = ANY($1::int[])
              AND archived_at IS NULL""",
        all_ids,
    )
    log.info(
        "conversation_archived",
        conv_id=numeric_id,
        cascaded=len(descendants),
        sql=result,
    )
    return {
        "ok": True,
        "id": numeric_id,
        "archived": True,
        "cascaded_ids": descendants,
    }


@app.post("/api/conversations/{conv_id:path}/unarchive")
async def conversation_unarchive(conv_id: str, _: Principal = Depends(get_principal)):
    """Reopens a conversation: back to Active. If a linked task was in a
    terminal status, the task is NOT reopened automatically — that is an
    explicit action (MCP tool reopen_task, D-57 phase 2.5) or a new task.

    D-72: cascade — archived descendants also go back to Active
    (idempotent)."""
    numeric_id = await _resolve_conv_id(conv_id)
    row = await db.fetch_one(
        "SELECT archived_at FROM messaging.conversations WHERE id = $1",
        numeric_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="conversation does not exist")
    if row["archived_at"] is None:
        raise HTTPException(status_code=409, detail="conversation is not archived")
    descendants = await _collect_descendant_conv_ids(numeric_id)
    all_ids = [numeric_id, *descendants]
    result = await db.execute(
        """UPDATE messaging.conversations
              SET archived_at = NULL
            WHERE id = ANY($1::int[])
              AND archived_at IS NOT NULL""",
        all_ids,
    )
    log.info(
        "conversation_unarchived",
        conv_id=numeric_id,
        cascaded=len(descendants),
        sql=result,
    )
    return {
        "ok": True,
        "id": numeric_id,
        "archived": False,
        "cascaded_ids": descendants,
    }


@app.delete("/api/conversations/{conv_id:path}")
async def conversation_delete_alias(conv_id: str, principal: Principal = Depends(get_principal)):
    """Permanent DELETE. Cascade deletes msgs + pending_asks + closed refs
    of each conversation. D-72: now also deletes descendants in cascade
    (ask_agent sub-conversations + convs of the same task). Active runners
    of the topics are cancelled via agent_ctrl before the DELETE, so they
    don't become ghosts holding the agent's pool slot."""
    if principal.user_id is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    numeric_id = await _resolve_conv_id(conv_id)
    descendants = await _collect_descendant_conv_ids(numeric_id)
    all_ids = [numeric_id, *descendants]
    # Cancel BEFORE the DELETE — the dispatcher resolves stream+topic from the
    # pg_notify payload, so the conv need not exist when the SIGTERM arrives,
    # but looking up stream/topic in the DB requires a live conv. Order matters.
    # silent=True: the conv is going away (DELETE), a confirmation would be noise.
    await _cancel_runners_for_convs(all_ids, silent=True)
    # Write a tombstone to block re-creation for 5min (D-72 fix): any
    # in-flight msg (agent still processing a turn, reactor with a pending
    # handoff, etc) arriving after the DELETE could auto-create the conv
    # again in the broker. Tombstone before the DELETE ensures the INSERT
    # matches stream_id/topic_name while the conv still exists.
    await db.execute(
        """INSERT INTO messaging.deleted_topics (stream_id, topic_name, deleted_at)
            SELECT c.stream_id, c.topic_name, now()
              FROM messaging.conversations c
             WHERE c.id = ANY($1::int[])
            ON CONFLICT (stream_id, topic_name)
              DO UPDATE SET deleted_at = now()""",
        all_ids,
    )
    # Aggregated trace snapshot into telemetry.events.metadata BEFORE the DELETE.
    # Reason: telemetry.live_events has an FK CASCADE to messaging.conversations
    # and is deleted along with the conv. The summary in telemetry.events survives
    # (FK SET NULL, migration 020) but the raw trace is gone. Here we keep
    # the aggregate ("how many thinkings, how many tool_use, which tools used")
    # in metadata — enough for task detail to keep showing effort after the
    # delete. The UPDATE matches via conversation_id OR via topic_slug (fallback
    # for rows whose conversation_id is not populated yet — until the runner is
    # rebuilt, most rows won't have the FK resolved).
    await db.execute(
        """WITH trace_agg AS (
            SELECT le.conversation_id,
                   s.name      AS stream_name,
                   c.topic_name,
                   COUNT(*) FILTER (WHERE kind = 'thinking')  AS thinking_count,
                   COUNT(*) FILTER (WHERE kind = 'tool_use')  AS tool_use_count,
                   jsonb_agg(DISTINCT le.data->>'name')
                     FILTER (WHERE kind = 'tool_use' AND le.data ? 'name') AS tools_used
              FROM telemetry.live_events le
              JOIN messaging.conversations c ON c.id = le.conversation_id
              JOIN messaging.streams       s ON s.id = c.stream_id
             WHERE le.conversation_id = ANY($1::int[])
             GROUP BY le.conversation_id, s.name, c.topic_name
        )
        UPDATE telemetry.events e
           SET metadata = COALESCE(e.metadata, '{}'::jsonb) || jsonb_build_object(
                 'trace_snapshot', jsonb_build_object(
                     'thinking_count', ta.thinking_count,
                     'tool_use_count', ta.tool_use_count,
                     'tools_used',     COALESCE(ta.tools_used, '[]'::jsonb),
                     'snapshot_at',    extract(epoch from now())
                 )
               )
          FROM trace_agg ta
         WHERE e.conversation_id = ta.conversation_id
            OR e.topic_slug = ta.stream_name || '__' || ta.topic_name""",
        all_ids,
    )
    result = await db.execute(
        "DELETE FROM messaging.conversations WHERE id = ANY($1::int[])",
        all_ids,
    )
    log.info(
        "conversation_deleted",
        conv_id=numeric_id,
        cascaded=len(descendants),
        sql=result,
    )
    return {"ok": True, "result": result, "cascaded_ids": descendants}


@app.post("/api/conversations/{conv_id:path}/close")
async def conversation_close_alias(conv_id: str, principal: Principal = Depends(get_principal)):
    from .broker import close_conversation
    numeric_id = await _resolve_conv_id(conv_id)
    return await close_conversation(numeric_id, principal)


@app.post("/api/conversations/{conv_id:path}/cancel")
async def conversation_cancel(conv_id: str, principal: Principal = Depends(get_principal)):
    """Cancels a pending turn on the agent (if it hasn't started calling Claude yet).

    D-29: fires a NOTIFY on the Postgres channel `agent_ctrl` with payload
    `{type, stream, topic, user_id}`. The agent filters by the stream it
    listens on and hands it to the dispatcher, which decides (nothing/too
    late/cancel + confirm). D-71: when the Claude CLI is running, the
    dispatcher now SIGTERMs the proc instead of answering "too late".
    """
    if principal.user_id is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    numeric_id = await _resolve_conv_id(conv_id)
    row = await db.fetch_one(
        """SELECT s.name AS stream, c.topic_name AS topic
             FROM messaging.conversations c
             JOIN messaging.streams s ON s.id = c.stream_id
            WHERE c.id = $1""",
        numeric_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="conversation does not exist")
    payload = json.dumps({
        "type": "cancel_topic",
        "stream": row["stream"],
        "topic": row["topic"],
        "user_id": principal.user_id,
    })
    # `pg_notify` is fire-and-forget — the agent may not be listening
    # (the "nothing to cancel" reply comes from the agent if it receives it).
    await db.execute("SELECT pg_notify('agent_ctrl', $1)", payload)
    return {"ok": True}


# ---------- D-71: runner state + retry ----------

# Threshold to mark a turn as "stuck": last live_event (run_start, thinking,
# tool_use, tool_result) more than X seconds ago with no run_end. Default
# 10min — the Claude CLI honors a 24h MCP_TOOL_TIMEOUT for ask_human/ask_agent,
# but those don't break run_start without intermediate events (thinking/tool_use
# are emitted during the pause). 10min covers a normal active-run cycle; beyond
# that it becomes a cancel/retry candidate.
RUNNER_STUCK_SEC = int(os.environ.get("RUNNER_STUCK_SEC", "600"))


async def _compute_runner_state(conv_id: int) -> dict:
    """Derives the current runner state for a conversation.

    Source: `messaging.runs` (single source of truth, migration 030) +
    `messaging.pending_asks` (active ask_human). The dual-write in the
    telemetry.live_events handler keeps `runs` in sync; the scheduler's
    reaper closes orphan runs as 'stale'.

    States:
      - `idle`: no run, or the last run finished (done/stale).
      - `running`: last run with status='running' and a fresh heartbeat.
      - `errored`: last run with status='error'.
      - `blocked_on_ask_human`: there is an unresolved pending_ask kind='ask_human'.
      - `stuck`: status='running' but an old heartbeat (> RUNNER_STUCK_SEC).
    """
    run = await db.fetch_one(
        """SELECT status, started_at, last_heartbeat_at, finished_at, exit_reason
             FROM messaging.runs
            WHERE conversation_id = $1
            ORDER BY started_at DESC
            LIMIT 1""",
        conv_id,
    )
    # D-111: blocked_on_ask_human = literally blocked on a human.
    # A pending ask_agent does not produce a "blocked" state for the UI (it is
    # an internal transition between agents, not a wait on a human).
    pending = await db.fetch_one(
        "SELECT asked_at FROM messaging.pending_asks "
        "WHERE conversation_id = $1 AND resolved_at IS NULL "
        "  AND kind = 'ask_human'",
        conv_id,
    )

    now = time.time()
    state = "idle"
    since = None
    last_error = None
    stuck = False

    # D-84: awaiting_human takes absolute precedence over running/stuck.
    # When there is a pending_ask, the runner may be alive, blocked in the
    # callback, but operationally nothing moves until the human answers.
    if pending is not None:
        state = "awaiting_human"
        since = pending["asked_at"]
    elif run is not None:
        if run["status"] == "running":
            since = run["started_at"]
            elapsed = now - run["last_heartbeat_at"].timestamp()
            if elapsed > RUNNER_STUCK_SEC:
                state = "stuck"
                stuck = True
            else:
                state = "running"
        elif run["status"] == "error":
            state = "errored"
            since = run["finished_at"] or run["started_at"]
            last_error = run["exit_reason"]
        # done / stale → idle (default)

    can_retry = state in ("errored", "stuck")
    # Cancel only makes sense while computation is in progress (Claude CLI
    # active, not blocked on ask_human). `blocked_on_ask_human` has the CLI
    # alive but blocked in the broker callback — a SIGTERM there would leave
    # pending_ask dangling and confuse the user ("cancel" sounds like
    # "interrupt work", but no work is happening, just waiting).
    # Correct UX in that state: answer the question or archive.
    can_cancel = state in ("running", "stuck")
    return {
        "state": state,
        "since": since.isoformat() if since is not None else None,
        "last_error": last_error,
        "can_retry": can_retry,
        "can_cancel": can_cancel,
        "stuck": stuck,
    }


@app.get("/api/conversations/{conv_id:path}/runner-state")
async def conversation_runner_state(
    conv_id: str, _: Principal = Depends(get_principal),
):
    """Returns the current runner state for a conversation.

    Used by the PWA to render the state badge and enable the Retry/Cancel
    buttons. Invalidated via SSE when run_start/run_end/thinking arrives.
    """
    numeric_id = await _resolve_conv_id(conv_id)
    return await _compute_runner_state(numeric_id)


@app.post("/api/conversations/{conv_id:path}/retry")
async def conversation_retry(
    conv_id: str, principal: Principal = Depends(get_principal),
):
    """Reprocesses the topic's last turn as if it were a new message.

    D-71: finds the last message whose sender is not the stream's own bot
    (avoids a loop — does not re-fire the agent's own reply as a trigger)
    and re-emits `pg_notify('msg_stream_<sid>', <payload>)`. The agent's
    `_on_notify` callback does not dedup by message_id, so it runs the
    handler again — same prompt, current session_id in the DB (may be None
    after D-70 recovery, which triggers a fresh start).

    Guard: only allowed if `can_retry` (errored/stuck). Otherwise 409 — avoids
    accidental double-dispatch on an active run.
    """
    if principal.user_id is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    numeric_id = await _resolve_conv_id(conv_id)
    state = await _compute_runner_state(numeric_id)
    if not state["can_retry"]:
        raise HTTPException(
            status_code=409,
            detail=f"retry unavailable in current state: {state['state']}",
        )
    # Fetch conversation + stream_id + the stream's bot name (= agent name,
    # by convention). We ignore that bot's messages to find the last
    # external trigger.
    conv = await db.fetch_one(
        """SELECT s.id AS stream_id, s.name AS stream_name, c.topic_name AS topic
             FROM messaging.conversations c
             JOIN messaging.streams s ON s.id = c.stream_id
            WHERE c.id = $1""",
        numeric_id,
    )
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation does not exist")
    # Last msg whose sender is not the agent itself (bot with agent_name =
    # stream_name). Fallback: any last msg.
    trigger = await db.fetch_one(
        """SELECT m.id, m.sender_id, m.conversation_id
             FROM messaging.messages m
             JOIN messaging.users u ON u.id = m.sender_id
            WHERE m.conversation_id = $1
              AND NOT (u.kind = 'bot' AND u.agent_name = $2)
            ORDER BY m.id DESC LIMIT 1""",
        numeric_id, conv["stream_name"],
    )
    if trigger is None:
        # Fallback: any last msg (case of a conversation with only the
        # agent's own msgs — rare but possible in automation loops).
        trigger = await db.fetch_one(
            "SELECT id, sender_id, conversation_id FROM messaging.messages "
            "WHERE conversation_id = $1 ORDER BY id DESC LIMIT 1",
            numeric_id,
        )
        if trigger is None:
            raise HTTPException(
                status_code=400,
                detail="conversation has no messages to reprocess",
            )
    # Re-emit the same payload shape the `messaging.notify_message` trigger
    # produces (see migrations/007_notify_payload_slim.sql).
    payload = json.dumps({
        "id": trigger["id"],
        "conversation_id": trigger["conversation_id"],
        "sender_id": trigger["sender_id"],
    })
    await db.execute(
        "SELECT pg_notify('msg_stream_' || $1, $2)",
        str(conv["stream_id"]), payload,
    )
    log.info(
        "web.retry_dispatched",
        conv_id=numeric_id, message_id=trigger["id"],
        stream=conv["stream_name"], topic=conv["topic"],
    )
    return {"ok": True, "dispatched": True, "message_id": trigger["id"]}


# ---------- Auth ----------

# D-95 followup: brute-force throttle.
#
# In-memory state per email — single-process (uvicorn 1 worker default),
# lost on restart (accepted: a brute-forcer who restarts web to reset the
# counter already had to get into the host, game over). The counter is a
# list of failure timestamps, with a sliding window of _LOGIN_WINDOW_SEC.
# 3+ failures within the window apply a 2^(count-2)s delay capped at 30s,
# before returning 401. Success resets that email's counter.
#
# The dummy hash below is used for a constant-time response when the email
# does not exist — without it, an attacker measures latency (no-bcrypt vs
# bcrypt) and enumerates which emails have an account.
_LOGIN_WINDOW_SEC = 900  # 15 minutes
_LOGIN_THROTTLE_AFTER = 3
_LOGIN_DELAY_CAP_SEC = 30
_login_failures: dict[str, list[float]] = {}
# bcrypt hash of a random string — never matches a real user password
_DUMMY_PWD_HASH = bcrypt.hashpw(os.urandom(32), bcrypt.gensalt()).decode()


def _login_failure_count(email: str) -> int:
    """How many consecutive failures within the window. Side effect: prunes
    the email's old entries."""
    now = time.time()
    cutoff = now - _LOGIN_WINDOW_SEC
    fails = [t for t in _login_failures.get(email, []) if t > cutoff]
    if fails:
        _login_failures[email] = fails
    else:
        _login_failures.pop(email, None)
    return len(fails)


def _record_login_failure(email: str) -> int:
    """Records a failure and returns the updated count."""
    now = time.time()
    fails = _login_failures.get(email, [])
    cutoff = now - _LOGIN_WINDOW_SEC
    fails = [t for t in fails if t > cutoff]
    fails.append(now)
    _login_failures[email] = fails
    return len(fails)


def _clear_login_failures(email: str) -> None:
    _login_failures.pop(email, None)


def _real_client_ip(request: Request) -> str:
    """Extracts the client's real IP.

    Behind cloudflared, request.client.host is the cloudflared container's
    IP (docker bridge network); the real IP comes in the `CF-Connecting-IP`
    header. Without cloudflared (direct access to 9090),
    request.client.host ALREADY is the real IP.

    Header trust is CONDITIONAL: we only trust CF-Connecting-IP
    / X-Forwarded-For when the source is private/loopback (cloudflared
    on the docker network or localhost). If the request came directly from
    a public IP, we ignore the header — anyone could spoof it.
    """
    direct = request.client.host if request.client else None
    trust_proxy_headers = False
    if direct:
        try:
            from ipaddress import ip_address
            ip = ip_address(direct)
            trust_proxy_headers = ip.is_private or ip.is_loopback
        except ValueError:
            trust_proxy_headers = False
    if trust_proxy_headers:
        cf = request.headers.get("cf-connecting-ip")
        if cf:
            return cf
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return direct or "?"


@app.post("/api/auth/login")
async def auth_login(payload: dict, request: Request, response: Response):
    """Human login: bcrypt check + create session + set HttpOnly cookie.

    Anti-brute-force (D-95): an in-memory counter per email applies a
    progressive delay after 3 failures within a 15min window. Constant-time
    bcrypt (runs even when the user does not exist) removes enumeration via
    timing.
    """
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    if not email or not password:
        raise HTTPException(status_code=400, detail="email and password are required")

    # Throttle BEFORE the query/bcrypt — an attacker gains nothing from 50 reqs/sec.
    fail_count = _login_failure_count(email)
    if fail_count >= _LOGIN_THROTTLE_AFTER:
        delay = min(2 ** (fail_count - _LOGIN_THROTTLE_AFTER + 1), _LOGIN_DELAY_CAP_SEC)
        log.warning(
            "web.auth.throttled",
            email=email, ip=_real_client_ip(request),
            fail_count=fail_count, delay_sec=delay,
        )
        await asyncio.sleep(delay)

    row = await db.fetch_one(
        """SELECT id, username, password_hash FROM messaging.users
            WHERE LOWER(email) = $1 AND kind = 'human'""",
        email,
    )
    # Constant-time: run bcrypt even if the user does not exist or hash is NULL.
    # A 401 response always goes through ~100ms of bcrypt, indistinguishable
    # from the "user exists, wrong password" case.
    target_hash = row["password_hash"] if (row and row["password_hash"]) else _DUMMY_PWD_HASH
    try:
        bcrypt_ok = bcrypt.checkpw(password.encode(), target_hash.encode())
    except Exception:
        bcrypt_ok = False
    ok = bool(row and row["password_hash"] and bcrypt_ok)

    if not ok:
        new_count = _record_login_failure(email)
        log.warning(
            "web.auth.login_failed",
            email=email, ip=_real_client_ip(request),
            fail_count=new_count, user_exists=bool(row),
        )
        raise HTTPException(status_code=401, detail="invalid credentials")

    _clear_login_failures(email)
    ua = request.headers.get("user-agent")
    token, expires_at = await create_session(row["id"], user_agent=ua)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_DAYS * 24 * 3600,
        httponly=True,
        secure=cookie_secure(),
        samesite="lax",
        path="/",
    )
    log.info(
        "web.auth.login", user_id=row["id"], username=row["username"],
        ip=_real_client_ip(request),
    )
    return {"ok": True, "username": row["username"], "expires_at": expires_at.isoformat()}


@app.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response):
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        await delete_session(cookie)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
async def auth_me(principal: Principal = Depends(get_principal)):
    return {
        "user_id": principal.user_id,
        "username": principal.username,
        "kind": principal.kind,
        "is_admin": principal.is_admin,
    }


@app.post("/api/auth/set-password")
async def auth_set_password(
    payload: dict,
    principal: Principal = Depends(auth_mod.require_admin),
):
    """Sets or updates the current admin's password. When a password is set,
    dev_bypass is disabled automatically — otherwise the password has no
    effect (the bypass grants admin without login)."""
    pwd = (payload.get("password") or "").strip()
    if len(pwd) < 8:
        raise HTTPException(status_code=400, detail="password must be at least 8 chars")
    import bcrypt as _bc
    hashed = _bc.hashpw(pwd.encode("utf-8"), _bc.gensalt(rounds=12)).decode("ascii")
    await db.execute(
        "UPDATE messaging.users SET password_hash = $1 WHERE id = $2",
        hashed, principal.user_id,
    )
    from . import app_settings as _s
    await _s.set("auth_dev_bypass", {"enabled": False}, user_id=principal.user_id)
    log.info("auth.password_set", user_id=principal.user_id)
    return {"ok": True}


# ---------- Tasks (unified view across conversations) ----------
# Schema lives in Postgres (tasks.tasks + tasks.phases + tasks.worktrees,
# migration 006 — D-53). Artifacts (.md) stay in company/tasks/<slug>/
# because agents write them via the Write tool and the PWA renders them.
# Archiving is a soft-delete (archived_at); delete purges the task + phases
# (CASCADE) + orchestrator.events + the artifacts directory.

_TASKS_ARTIFACT_DIR = Path("/workspace/company/tasks")

import re as _re
_SLUG_RE = _re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
_TERMINAL_STATUSES = {"done", "halt", "human_review", "blocked"}


def _validate_slug(slug: str) -> str:
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="invalid slug format")
    return slug


async def _fetch_task_row(slug: str) -> dict | None:
    _validate_slug(slug)
    row = await db.fetch_one(
        """SELECT id, slug, title, workflow, status, current_step, current_agent,
                  complexity, impact, difficulty, origin, origin_stream, origin_topic,
                  blocked_reason, metadata_extra, archived_at, created_at, updated_at
             FROM tasks.tasks WHERE slug = $1""",
        slug,
    )
    return dict(row) if row else None


@app.get("/api/tasks")
async def tasks_list(
    include_archived: int = 0,
    _: Principal = Depends(get_principal),
):
    """Lists tasks from Postgres (schema tasks.*, migration 006). Ordered by
    updated_at desc. `include_archived=1` also includes the soft-deleted ones
    (archived_at IS NOT NULL). Fields compatible with the PWA's legacy shape
    for zero frontend changes."""
    where_clause = "" if include_archived else "WHERE t.archived_at IS NULL"
    rows = await db.fetch_all(
        f"""SELECT t.slug, t.title, t.status, t.current_step, t.current_agent,
                   t.workflow, t.updated_at, t.archived_at,
                   COALESCE((SELECT COUNT(*) FROM tasks.phases p
                              WHERE p.task_id = t.id AND p.completed_at IS NOT NULL), 0) AS phases_count
              FROM tasks.tasks t
              {where_clause}
             ORDER BY t.updated_at DESC""",
    )
    items = [
        {
            "slug": r["slug"],
            "title": r["title"],
            "status": r["status"],
            "current_step": r["current_step"],
            "current_agent": r["current_agent"],
            "workflow": r["workflow"],
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            "phases_count": int(r["phases_count"] or 0),
            "archived": r["archived_at"] is not None,
        }
        for r in rows
    ]
    return {"items": items}


@app.post("/api/tasks/{slug}/archive")
async def task_archive(slug: str, _: Principal = Depends(get_principal)):
    """Soft-delete: sets archived_at = now() if the status is terminal.
    A live task (in_progress) cannot be archived — the orchestrator may
    still be dispatching."""
    row = await _fetch_task_row(slug)
    if row is None:
        raise HTTPException(status_code=404, detail=f"task '{slug}' not found")
    if row["archived_at"] is not None:
        raise HTTPException(status_code=409, detail="task is already archived")
    if row["status"] not in _TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"can only archive task in terminal status; current status: {row['status']!r}",
        )
    await db.execute(
        "UPDATE tasks.tasks SET archived_at = now() WHERE slug = $1",
        slug,
    )
    log.info("task_archived", slug=slug, status=row["status"])
    return {"ok": True, "slug": slug, "archived": True}


@app.post("/api/tasks/{slug}/unarchive")
async def task_unarchive(slug: str, _: Principal = Depends(get_principal)):
    """Unset archived_at."""
    row = await _fetch_task_row(slug)
    if row is None:
        raise HTTPException(status_code=404, detail=f"task '{slug}' not found")
    if row["archived_at"] is None:
        raise HTTPException(status_code=409, detail="task is not archived")
    await db.execute(
        "UPDATE tasks.tasks SET archived_at = NULL WHERE slug = $1",
        slug,
    )
    log.info("task_unarchived", slug=slug)
    return {"ok": True, "slug": slug, "archived": False}


@app.delete("/api/tasks/{slug}")
async def task_delete(slug: str, _: Principal = Depends(get_principal)):
    """Permanent delete. Only allowed if the task is ARCHIVED.
    Deletes the row (CASCADE on phases/worktrees), purges the task's
    orchestrator.events, and removes the artifacts directory from the filesystem."""
    import shutil as _shutil
    row = await _fetch_task_row(slug)
    if row is None:
        raise HTTPException(status_code=404, detail=f"task '{slug}' not found")
    if row["archived_at"] is None:
        raise HTTPException(
            status_code=409,
            detail="task is active; archive it before deleting",
        )
    events_purged = await db.execute(
        "DELETE FROM orchestrator.events WHERE task_slug = $1",
        slug,
    )
    await db.execute("DELETE FROM tasks.tasks WHERE slug = $1", slug)
    # Remove the artifacts dir (if it exists). Safety: the resolved path must
    # start with the prefix — defense against a slug with .. (already validated
    # by _SLUG_RE, but defense in depth).
    artifacts = _TASKS_ARTIFACT_DIR / slug
    if artifacts.is_dir() and str(artifacts.resolve()).startswith(
        str(_TASKS_ARTIFACT_DIR.resolve()) + os.sep
    ):
        _shutil.rmtree(artifacts, ignore_errors=True)
    log.info("task_deleted", slug=slug, events_purged=str(events_purged))
    return {"ok": True, "slug": slug, "deleted": True}


# ---------- Workflows (D-57 — dynamic progress bar in the PWA) ----------
# Exposes the instance's workflows yaml so the frontend can render the
# progress bar without knowing the vocabulary. The framework stays agnostic:
# it only resolves structure (happy-path order, terminals), never hardcodes
# step names.

import yaml as _yaml

_WORKFLOWS_YAML_PATH = Path("/workspace/company/workflows.yaml")
_WORKFLOW_TERMINALS = {"done", "halt", "human_review"}
_WORKFLOW_NAME_RE = _re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_WORKFLOW_VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_WORKFLOWS_YAML_MAX_BYTES = 256 * 1024  # higher cap than company files — workflow yaml grows with steps
_WORKFLOWS_YAML_HEADER = (
    "# workflows.yaml — this instance's phase taxonomy.\n"
    "#\n"
    "# This file is managed by the PWA (Settings -> Workflows). Editing it\n"
    "# directly on disk works, but comments and formatting will be\n"
    "# rewritten on the next save via the UI. Prefer the interface.\n"
    "#\n"
    "# The framework only knows the terminals (done | halt | human_review).\n"
    "# Everything else (step names, default agents, artifacts, transitions)\n"
    "# is this instance's convention.\n"
    "\n"
)


def _load_workflows_raw() -> dict:
    if not _WORKFLOWS_YAML_PATH.exists():
        return {}
    with _WORKFLOWS_YAML_PATH.open("r", encoding="utf-8") as f:
        raw = _yaml.safe_load(f) or {}
    return raw.get("workflows") or {}


def _load_workflows_full() -> dict:
    """Returns the whole YAML document (not only the `workflows` key).
    Used by the write endpoints, which must preserve metadata outside
    `workflows:` if the instance added any."""
    if not _WORKFLOWS_YAML_PATH.exists():
        return {"workflows": {}}
    with _WORKFLOWS_YAML_PATH.open("r", encoding="utf-8") as f:
        raw = _yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("workflows", {})
    if not isinstance(raw["workflows"], dict):
        raw["workflows"] = {}
    return raw


class _WorkflowYamlDumper(_yaml.SafeDumper):
    """Custom SafeDumper that renders multiline strings in block style (`|`)
    instead of flow style with escaped `\\n`. Used so `instructions`
    (multiline markdown) stays readable in the on-disk yaml."""


def _str_representer(dumper, data: str):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_WorkflowYamlDumper.add_representer(str, _str_representer)


def _dump_workflows_yaml(full: dict) -> str:
    """Serializes with a fixed header + custom dumper, preserving order and
    rendering multiline markdown as a block scalar."""
    body = _yaml.dump(
        full,
        Dumper=_WorkflowYamlDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    return _WORKFLOWS_YAML_HEADER + body


def _write_workflows_yaml(full: dict) -> int:
    text = _dump_workflows_yaml(full)
    encoded = text.encode("utf-8")
    if len(encoded) > _WORKFLOWS_YAML_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"workflows.yaml exceeds {_WORKFLOWS_YAML_MAX_BYTES} bytes",
        )
    _WORKFLOWS_YAML_PATH.parent.mkdir(parents=True, exist_ok=True)
    _WORKFLOWS_YAML_PATH.write_text(text, encoding="utf-8")
    return len(encoded)


def _normalize_workflow_body(body: dict) -> dict:
    """Normalizes the input dict for deterministic yaml serialization.

    - Drops unknown top-level fields, keeping only `initial_step`,
      `steps` and `description` (optional).
    - In each step, keeps only `agent`, `artifact`, `next`, `instructions`.
    - Missing or empty-string `agent`/`artifact`/`instructions` => omitted
      from the yaml (equivalent to None => "requires explicit next_agent" /
      "no default artifact" / "no phase instructions block").
    - `next` is always a list of unique strings, order preserved.
    - `instructions` accepts a multiline string (markdown). Capped at 32 KB to
      avoid an abusive payload in the prompt.
    """
    out: dict = {}
    if isinstance(body.get("description"), str) and body["description"].strip():
        out["description"] = body["description"].strip()
    # orchestrator (D-110): agent that owns the supervisor conv; fixed root
    # of the conv hierarchy for the task's lifetime. Optional — without it,
    # the framework falls back to initial_step.agent (compat with pre-D-110 workflows).
    orchestrator = body.get("orchestrator")
    if isinstance(orchestrator, str) and orchestrator.strip():
        out["orchestrator"] = orchestrator.strip()
    out["initial_step"] = str(body.get("initial_step") or "")
    steps_in = body.get("steps") or {}
    steps_out: dict = {}
    if isinstance(steps_in, dict):
        iterator = steps_in.items()
    elif isinstance(steps_in, list):
        # accepts [{name, agent, artifact, next, instructions}, ...] to make the frontend easier
        iterator = [(s.get("name"), s) for s in steps_in if isinstance(s, dict)]
    else:
        iterator = []
    for s_name, s_body in iterator:
        if not isinstance(s_name, str) or not isinstance(s_body, dict):
            continue
        entry: dict = {}
        agent = s_body.get("agent")
        if isinstance(agent, str) and agent.strip():
            entry["agent"] = agent.strip()
        artifact = s_body.get("artifact")
        if isinstance(artifact, str) and artifact.strip():
            entry["artifact"] = artifact.strip()
        next_raw = s_body.get("next") or []
        if not isinstance(next_raw, list):
            next_raw = []
        seen: set[str] = set()
        next_list: list[str] = []
        for n in next_raw:
            if not isinstance(n, str):
                continue
            n = n.strip()
            if not n or n in seen:
                continue
            seen.add(n)
            next_list.append(n)
        entry["next"] = next_list
        instructions = s_body.get("instructions")
        if isinstance(instructions, str):
            cleaned = instructions.replace("\r\n", "\n").rstrip()
            if cleaned.strip():
                if len(cleaned.encode("utf-8")) > 32 * 1024:
                    raise HTTPException(
                        status_code=413,
                        detail=f"step {s_name!r}: instructions exceeds 32 KB",
                    )
                entry["instructions"] = cleaned
        overrides = _normalize_step_overrides(s_body.get("overrides"))
        if overrides:
            entry["overrides"] = overrides
        if bool(s_body.get("fresh_session", False)):
            entry["fresh_session"] = True
        steps_out[s_name] = entry
    out["steps"] = steps_out
    return out


def _normalize_step_overrides(raw: Any) -> dict:
    """Normalizes a step's `overrides` sub-dict.

    Nested whitelist: model, effort, memory.{enabled, auto_inject_limit}.
    Unknown keys are silently dropped. Strings stripped + empty => omitted.
    Numbers/bools kept as-is — semantic validation in _validate_step_overrides.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    model = raw.get("model")
    if isinstance(model, str) and model.strip():
        out["model"] = model.strip()
    effort = raw.get("effort")
    if isinstance(effort, str) and effort.strip():
        out["effort"] = effort.strip()
    mem_raw = raw.get("memory")
    if isinstance(mem_raw, dict):
        mem_out: dict = {}
        if "enabled" in mem_raw:
            mem_out["enabled"] = mem_raw["enabled"]
        if "auto_inject_limit" in mem_raw:
            mem_out["auto_inject_limit"] = mem_raw["auto_inject_limit"]
        if mem_out:
            out["memory"] = mem_out
    return out


def _validate_workflow_body(name: str, body: dict) -> None:
    """Validates the normalized body. Raises HTTPException(400) on error."""
    if not isinstance(name, str) or not _WORKFLOW_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="invalid name: use lowercase/digits/-/_ (1-64 chars, starts with alphanumeric)",
        )
    if name in _WORKFLOW_TERMINALS:
        raise HTTPException(
            status_code=400,
            detail=f"name collides with reserved framework terminal: {name}",
        )
    steps = body.get("steps") or {}
    if not isinstance(steps, dict) or not steps:
        raise HTTPException(status_code=400, detail="steps is required and cannot be empty")
    for s_name in steps.keys():
        if not isinstance(s_name, str) or not _WORKFLOW_NAME_RE.match(s_name):
            raise HTTPException(
                status_code=400,
                detail=f"invalid step name: {s_name!r} (use lowercase/digits/-/_)",
            )
        if s_name in _WORKFLOW_TERMINALS:
            raise HTTPException(
                status_code=400,
                detail=f"step name collides with reserved terminal: {s_name}",
            )
    initial = body.get("initial_step") or ""
    if not initial or initial not in steps:
        raise HTTPException(
            status_code=400,
            detail=f"initial_step {initial!r} must exist in steps",
        )
    valid_targets = set(steps.keys()) | _WORKFLOW_TERMINALS
    for s_name, s_body in steps.items():
        nxt = s_body.get("next") or []
        if not nxt:
            raise HTTPException(
                status_code=400,
                detail=f"step {s_name!r}: next cannot be empty (use [done]/[halt]/... if terminal)",
            )
        for target in nxt:
            if target not in valid_targets:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"step {s_name!r}: next {target!r} does not exist. "
                        f"Valid targets: declared steps ({sorted(steps.keys())}) "
                        f"or terminals ({sorted(_WORKFLOW_TERMINALS)})."
                    ),
                )
        if "overrides" in s_body:
            _validate_step_overrides(s_name, s_body["overrides"])


def _validate_step_overrides(s_name: str, overrides: Any) -> None:
    """Validates semantic values in the already-normalized overrides. 400 on error.

    The workflow is authoritative: it does not check whether the model exists
    on the agent or whether effort exceeds a ceiling — runtime resolves that.
    Here we only check shape/enum.
    """
    if not isinstance(overrides, dict):
        raise HTTPException(
            status_code=400,
            detail=f"step {s_name!r}: overrides must be an object",
        )
    if "model" in overrides:
        model = overrides["model"]
        if not isinstance(model, str) or not model.strip():
            raise HTTPException(
                status_code=400,
                detail=f"step {s_name!r}: overrides.model must be non-empty string",
            )
    if "effort" in overrides:
        effort = overrides["effort"]
        if effort not in _WORKFLOW_VALID_EFFORTS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"step {s_name!r}: overrides.effort {effort!r} is invalid. "
                    f"Accepted values: {list(_WORKFLOW_VALID_EFFORTS)}"
                ),
            )
    if "memory" in overrides:
        mem = overrides["memory"]
        if not isinstance(mem, dict):
            raise HTTPException(
                status_code=400,
                detail=f"step {s_name!r}: overrides.memory must be an object",
            )
        if "enabled" in mem and not isinstance(mem["enabled"], bool):
            raise HTTPException(
                status_code=400,
                detail=f"step {s_name!r}: overrides.memory.enabled must be a boolean",
            )
        if "auto_inject_limit" in mem:
            lim = mem["auto_inject_limit"]
            if not isinstance(lim, int) or isinstance(lim, bool) or lim < 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"step {s_name!r}: overrides.memory.auto_inject_limit must be int >= 0",
                )


def _happy_path_order(initial_step: str, steps_raw: dict) -> list[str]:
    """Happy-path topo-sort: from initial_step, follow the first `next` that is
    not a terminal, stopping on a terminal or a loop. The order of the yaml
    `next` list is preserved (yaml.safe_load returns a list, not a set).

    If the graph branches (next has several non-terminal steps), we take the
    first — it is only a heuristic to render linearly; the real authority
    over "next" still comes from the agent via complete_phase."""
    ordered: list[str] = []
    visited: set[str] = set()
    current: str | None = initial_step
    while current and current not in visited and current not in _WORKFLOW_TERMINALS:
        if current not in steps_raw:
            break
        visited.add(current)
        ordered.append(current)
        next_list = (steps_raw[current] or {}).get("next") or []
        next_step: str | None = None
        for n in next_list:
            if n not in _WORKFLOW_TERMINALS:
                next_step = n
                break
        current = next_step
    return ordered


def _serialize_workflow(name: str, wf_body: dict) -> dict:
    """Converts the dict read from yaml into the shape the frontend consumes
    (same shape as GET /api/workflows/{name}): includes steps_ordered."""
    steps_raw = wf_body.get("steps") or {}
    initial_step = wf_body.get("initial_step") or ""
    steps_out: dict[str, dict] = {}
    for s_name, s_body in steps_raw.items():
        if not isinstance(s_body, dict):
            continue
        steps_out[s_name] = {
            "agent": s_body.get("agent"),
            "artifact": s_body.get("artifact"),
            "next": list(s_body.get("next") or []),
            "instructions": s_body.get("instructions"),
            "fresh_session": bool(s_body.get("fresh_session", False)),
        }
    return {
        "name": name,
        "initial_step": initial_step,
        "orchestrator": wf_body.get("orchestrator"),
        "steps_ordered": _happy_path_order(initial_step, steps_raw),
        "steps": steps_out,
        "terminals": sorted(_WORKFLOW_TERMINALS),
        "description": wf_body.get("description"),
    }


@app.get("/api/workflows")
async def workflows_list(expand: int = 0, _: Principal = Depends(get_principal)):
    """Lists workflows. `expand=1` returns the full body (steps+transitions)
    inline, otherwise only names — old default kept for existing clients
    (progress bar)."""
    wfs = _load_workflows_raw()
    if expand:
        items = [_serialize_workflow(n, b) for n, b in wfs.items() if isinstance(b, dict)]
        return {"items": items, "terminals": sorted(_WORKFLOW_TERMINALS), "expanded": True}
    return {"items": list(wfs.keys()), "terminals": sorted(_WORKFLOW_TERMINALS)}


@app.get("/api/workflows/{name}")
async def workflow_get(name: str, _: Principal = Depends(get_principal)):
    """Returns the instance's workflow definition (no hardcoded semantics).
    The frontend renders the progress bar by iterating over `steps_ordered`.
    Terminals (`done`/`halt`/`human_review`) are returned separately because
    they are framework invariants, not instance ones."""
    wfs = _load_workflows_raw()
    wf = wfs.get(name)
    if not wf:
        raise HTTPException(status_code=404, detail=f"workflow '{name}' not declared in workflows.yaml")
    return _serialize_workflow(name, wf)


@app.put("/api/workflows/{name}")
async def workflow_upsert(
    name: str,
    payload: dict,
    _: Principal = Depends(get_principal),
):
    """Creates or updates a workflow. Body:
      { initial_step: str, steps: {name: {agent?, artifact?, next: [...]}}, description?: str }
    Validates structure, writes to disk. WorkflowRegistry re-reads on every
    load(), so the change applies immediately — no restart/reconcile."""
    normalized = _normalize_workflow_body(payload)
    _validate_workflow_body(name, normalized)
    full = _load_workflows_full()
    wfs = full.get("workflows") or {}
    wfs[name] = normalized
    full["workflows"] = wfs
    size = _write_workflows_yaml(full)
    log.info("workflows.upsert", name=name, size=size)
    return _serialize_workflow(name, normalized)


@app.delete("/api/workflows/{name}")
async def workflow_delete(name: str, _: Principal = Depends(get_principal)):
    full = _load_workflows_full()
    wfs = full.get("workflows") or {}
    if name not in wfs:
        raise HTTPException(status_code=404, detail=f"workflow '{name}' does not exist")
    del wfs[name]
    full["workflows"] = wfs
    size = _write_workflows_yaml(full)
    log.info("workflows.delete", name=name, size=size)
    return {"ok": True}


@app.post("/api/workflows/{name}/rename")
async def workflow_rename(
    name: str,
    payload: dict,
    _: Principal = Depends(get_principal),
):
    new_name = (payload.get("new_name") or "").strip()
    if not _WORKFLOW_NAME_RE.match(new_name):
        raise HTTPException(
            status_code=400,
            detail="invalid new_name: use lowercase/digits/-/_ (1-64 chars)",
        )
    if new_name in _WORKFLOW_TERMINALS:
        raise HTTPException(status_code=400, detail=f"new_name collides with terminal: {new_name}")
    if new_name == name:
        raise HTTPException(status_code=400, detail="new_name is same as current")
    full = _load_workflows_full()
    wfs = full.get("workflows") or {}
    if name not in wfs:
        raise HTTPException(status_code=404, detail=f"workflow '{name}' does not exist")
    if new_name in wfs:
        raise HTTPException(status_code=409, detail=f"workflow '{new_name}' already exists")
    # Preserve order: replace the key in place instead of appending at the end.
    renamed: dict = {}
    for k, v in wfs.items():
        if k == name:
            renamed[new_name] = v
        else:
            renamed[k] = v
    full["workflows"] = renamed
    size = _write_workflows_yaml(full)
    log.info("workflows.rename", old=name, new=new_name, size=size)
    return _serialize_workflow(new_name, renamed[new_name])


# ---------- Backlog (D-53) ----------
# Simple CRUD on the tasks.backlog schema. Access: admin (create/edit/promote
# are human actions) via the PWA. Agents use it via the backlog_* MCP tools.

@app.get("/api/backlog")
async def backlog_list(
    status: str | None = None,
    include_all: int = 0,
    _: Principal = Depends(get_principal),
):
    """Lists backlog items. Default: only status='open'. Pass status=X
    to filter or include_all=1 to return everything. Ordered by priority
    desc, updated_at desc.
    """
    if include_all:
        rows = await db.fetch_all(
            """SELECT slug, title, content, priority, impact, effort, status,
                      promoted_task_slug, created_by, created_at, updated_at
                 FROM tasks.backlog
                ORDER BY priority DESC, updated_at DESC""",
        )
    else:
        status = status or "open"
        rows = await db.fetch_all(
            """SELECT slug, title, content, priority, impact, effort, status,
                      promoted_task_slug, created_by, created_at, updated_at
                 FROM tasks.backlog
                WHERE status = $1
                ORDER BY priority DESC, updated_at DESC""",
            status,
        )
    return {
        "items": [
            {
                "slug": r["slug"], "title": r["title"], "content": r["content"],
                "priority": int(r["priority"] or 0),
                "impact": r["impact"], "effort": r["effort"],
                "status": r["status"], "promoted_task_slug": r["promoted_task_slug"],
                "created_by": r["created_by"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ]
    }


_BACKLOG_STATUSES = ("open", "draft", "in_progress", "promoted", "done", "discarded")


@app.post("/api/backlog")
async def backlog_create(payload: dict, principal: Principal = Depends(get_principal)):
    slug = (payload.get("slug") or "").strip()
    title = (payload.get("title") or "").strip()
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="invalid slug (kebab-case)")
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    priority = int(payload.get("priority") or 0)
    status = (payload.get("status") or "open").strip()
    if status not in _BACKLOG_STATUSES:
        raise HTTPException(status_code=400, detail=f"invalid status: {status!r}")
    await db.execute(
        """INSERT INTO tasks.backlog (slug, title, content, priority, impact, effort, status, created_by)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
           ON CONFLICT (slug) DO UPDATE SET
             title = EXCLUDED.title,
             content = EXCLUDED.content,
             priority = EXCLUDED.priority,
             impact = EXCLUDED.impact,
             effort = EXCLUDED.effort,
             status = EXCLUDED.status""",
        slug, title, payload.get("content") or "",
        priority, payload.get("impact"), payload.get("effort"), status,
        principal.username or "user",
    )
    return {"ok": True, "slug": slug}


@app.patch("/api/backlog/{slug}")
async def backlog_patch(slug: str, payload: dict, _: Principal = Depends(get_principal)):
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="invalid slug")
    if payload.get("status") and payload["status"] not in _BACKLOG_STATUSES:
        raise HTTPException(status_code=400, detail=f"invalid status: {payload['status']!r}")
    fields = []
    params: list = []
    idx = 1
    for col in ("title", "content", "impact", "effort", "status"):
        if col in payload and payload[col] is not None:
            fields.append(f"{col} = ${idx}")
            params.append(payload[col])
            idx += 1
    if "priority" in payload and payload["priority"] is not None:
        fields.append(f"priority = ${idx}")
        params.append(int(payload["priority"]))
        idx += 1
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    params.append(slug)
    row = await db.fetch_one(
        f"UPDATE tasks.backlog SET {', '.join(fields)} WHERE slug = ${idx} RETURNING slug, priority, status",
        *params,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"backlog item '{slug}' does not exist")
    return {"ok": True, "slug": row["slug"], "priority": int(row["priority"] or 0), "status": row["status"]}


@app.delete("/api/backlog/{slug}")
async def backlog_delete(slug: str, _: Principal = Depends(get_principal)):
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="invalid slug")
    row = await db.fetch_one(
        "DELETE FROM tasks.backlog WHERE slug = $1 RETURNING slug", slug,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"backlog item '{slug}' does not exist")
    return {"ok": True, "slug": slug, "deleted": True}


@app.post("/api/backlog/{slug}/promote")
async def backlog_promote_endpoint(slug: str, payload: dict, principal: Principal = Depends(get_principal)):
    """Promotes a backlog item to a task and dispatches it to the first agent.

    Body: { task_slug?, workflow?, next_agent?, initial_topic?, initial_step? }

    Mirrors the logic of the backlog_promote MCP tool (server.py). The conv
    hierarchy is anchored on the `orchestrator` declared by the workflow:
    the supervisor conv lives in `<orchestrator>/task-<slug>` (root) and the
    initial phase's conv becomes a child (Path A) or merges with the
    supervisor if `next_agent == orchestrator` (Path B). A workflow without
    a declared `orchestrator` falls back to `next_agent`/`initial_step.agent`
    — behavior equivalent to pre-D-110.
    """
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="invalid slug")
    task_slug = (payload.get("task_slug") or slug).strip()
    if not _SLUG_RE.match(task_slug):
        raise HTTPException(status_code=400, detail="invalid task_slug")
    workflow = payload.get("workflow")
    next_agent = payload.get("next_agent")
    initial_topic = (payload.get("initial_topic") or f"task-{task_slug}").strip()
    initial_step = payload.get("initial_step")
    # Resolve initial_step + default next_agent + the workflow's orchestrator.
    orchestrator: str | None = None
    if workflow:
        wfs = _load_workflows_raw()
        wf_def = wfs.get(workflow) or {}
        if not initial_step:
            initial_step = (wf_def.get("initial_step")
                            or wf_def.get("first_step")
                            or None)
        if not next_agent and initial_step:
            steps = wf_def.get("steps") or {}
            step_def = steps.get(initial_step) or {}
            next_agent = step_def.get("agent")
        orchestrator = wf_def.get("orchestrator")
    if not next_agent:
        raise HTTPException(
            status_code=400,
            detail="next_agent is required (pass explicitly or define workflow with initial_step.agent)",
        )
    # orchestrator: workflow field > fallback to next_agent.
    if not orchestrator:
        orchestrator = next_agent
    origin_stream = orchestrator
    origin_topic = initial_topic
    async with db.connection() as conn:
        async with conn.transaction():
            item = await conn.fetchrow(
                "SELECT title, content FROM tasks.backlog WHERE slug = $1 FOR UPDATE",
                slug,
            )
            if item is None:
                raise HTTPException(status_code=404, detail=f"backlog '{slug}' does not exist")
            existing = await conn.fetchval(
                "SELECT id FROM tasks.tasks WHERE slug = $1", task_slug,
            )
            if existing is None:
                await conn.execute(
                    """INSERT INTO tasks.tasks
                          (slug, title, workflow, status,
                           current_step, current_agent,
                           origin_stream, origin_topic)
                       VALUES ($1, $2, $3, 'in_progress', $4, $5, $6, $7)""",
                    task_slug, item["title"], workflow,
                    initial_step, next_agent,
                    origin_stream, origin_topic,
                )
            await conn.execute(
                """UPDATE tasks.backlog
                      SET status = 'promoted', promoted_task_slug = $2
                    WHERE slug = $1""",
                slug, task_slug,
            )
            payload_ev = {
                "task_slug": task_slug,
                "from_step": None,
                "from_agent": principal.username or "user",
                "artifact": None,
                "summary": f"promoted from backlog: {item['title']}",
                "next": "start",
                "next_agent": next_agent,
                "next_topic": initial_topic,
                "origin_stream": origin_stream,
                "origin_topic": origin_topic,
                "workflow": workflow,
                "backlog_slug": slug,
                "backlog_content": item["content"],
            }
            await conn.execute(
                """INSERT INTO orchestrator.events
                       (emitted_by, event_type, task_slug, payload)
                   VALUES ($1, 'phase_complete', $2, $3::jsonb)""",
                principal.username or "user", task_slug, json.dumps(payload_ev),
            )
    log.info("backlog_promoted",
             slug=slug, task_slug=task_slug,
             orchestrator=orchestrator, next_agent=next_agent)
    return {"ok": True, "backlog_slug": slug, "task_slug": task_slug,
            "orchestrator": orchestrator, "next_agent": next_agent}


@app.post("/api/backlog/{slug}/revert")
async def backlog_revert_endpoint(slug: str, principal: Principal = Depends(get_principal)):
    """Undoes a backlog item's promotion. Used when the human realizes
    they dispatched to the wrong agent and wants to re-promote — instead
    of replying in the active topic or running SQL by hand.

    Safety gate: refuses if the task already has completed phases OR if
    a human already posted in the conv (human engaged = not a dispatch
    mistake, it is work in progress). Auto-cancels pending_asks, deletes
    the task's conv (cascade msgs), deletes the task row and sets the
    backlog back to status='open'. Worktrees are only deleted from the
    database — files on the filesystem are the agent's responsibility
    (register_worktree does not create them automatically).
    """
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="invalid slug")
    async with db.connection() as conn:
        async with conn.transaction():
            bl = await conn.fetchrow(
                "SELECT slug, status, promoted_task_slug FROM tasks.backlog "
                "WHERE slug = $1 FOR UPDATE",
                slug,
            )
            if bl is None:
                raise HTTPException(status_code=404, detail=f"backlog '{slug}' does not exist")
            if bl["status"] != "promoted" or not bl["promoted_task_slug"]:
                raise HTTPException(
                    status_code=409,
                    detail="item is not in 'promoted' state — nothing to undo",
                )
            task_slug = bl["promoted_task_slug"]

            task = await conn.fetchrow(
                "SELECT id, status FROM tasks.tasks WHERE slug = $1 FOR UPDATE",
                task_slug,
            )
            if task is not None:
                # Safety: a task with completed phases OR with human
                # participation cannot be reverted without losing work.
                phases_done = await conn.fetchval(
                    "SELECT count(*) FROM tasks.phases WHERE task_id = $1 AND completed_at IS NOT NULL",
                    task["id"],
                )
                if phases_done and phases_done > 0:
                    raise HTTPException(
                        status_code=409,
                        detail=f"task already has {phases_done} completed phase(s) — cannot revert without losing work",
                    )
                human_msgs = await conn.fetchval(
                    """SELECT count(*) FROM messaging.messages m
                         JOIN messaging.users u ON u.id = m.sender_id
                         JOIN messaging.conversations c ON c.id = m.conversation_id
                        WHERE c.topic_name = $1
                          AND NOT u.is_bot
                          AND u.username != 'system-bot'
                          AND u.username != 'orchestrator'""",
                    f"task-{task_slug}",
                )
                if human_msgs and human_msgs > 0:
                    raise HTTPException(
                        status_code=409,
                        detail="human already posted in task conversation — reply there instead of reverting",
                    )

                # Cancel pending_asks of all the task's convs.
                await conn.execute(
                    """UPDATE messaging.pending_asks pa
                          SET resolved_at = now()
                         WHERE pa.conversation_id IN (
                           SELECT c.id FROM messaging.conversations c
                            WHERE c.topic_name = $1
                         ) AND pa.resolved_at IS NULL""",
                    f"task-{task_slug}",
                )
                # Delete the task's convs (cascade deletes messages + pending_asks).
                await conn.execute(
                    """DELETE FROM messaging.messages
                        WHERE conversation_id IN (
                          SELECT id FROM messaging.conversations
                           WHERE topic_name = $1
                        )""",
                    f"task-{task_slug}",
                )
                await conn.execute(
                    """DELETE FROM messaging.pending_asks
                        WHERE conversation_id IN (
                          SELECT id FROM messaging.conversations
                           WHERE topic_name = $1
                        )""",
                    f"task-{task_slug}",
                )
                await conn.execute(
                    "DELETE FROM messaging.conversations WHERE topic_name = $1",
                    f"task-{task_slug}",
                )
                # Delete phases/worktrees + task.
                await conn.execute("DELETE FROM tasks.phases WHERE task_id = $1", task["id"])
                await conn.execute("DELETE FROM tasks.worktrees WHERE task_id = $1", task["id"])
                await conn.execute("DELETE FROM tasks.tasks WHERE id = $1", task["id"])

            await conn.execute(
                """UPDATE tasks.backlog
                      SET status = 'open', promoted_task_slug = NULL, updated_at = now()
                    WHERE slug = $1""",
                slug,
            )
    log.info("backlog_reverted", slug=slug, task_slug=task_slug, by=principal.username or "user")
    return {"ok": True, "backlog_slug": slug, "task_slug": task_slug}


@app.post("/api/backlog/{slug}/force-reset")
async def backlog_force_reset_endpoint(slug: str, principal: Principal = Depends(get_principal)):
    """D-101: Wipes out a task in any state and returns the backlog item
    to 'open', ready to be re-promoted. Unlike /revert, it ignores the
    safety gates (completed phases, human message) — explicit use when
    the human wants to start over after finding a design flaw or
    switching to another approach.

    Operations (transaction):
      1. Cancel pending_asks, delete the task's messages/conversations
         (topic-name match across all streams).
      2. Delete tasks.phases, tasks.worktrees, tasks.tasks (CASCADE +
         explicit for resilience).
      3. Purge orchestrator.events matching task_slug.
      4. Delete company/tasks/<task_slug>/ on the filesystem.
      5. Revert the backlog row to status='open', promoted_task_slug=NULL.

    The frontend must confirm via a modal — the endpoint has no soft mode."""
    import shutil as _shutil
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="invalid slug")
    async with db.connection() as conn:
        async with conn.transaction():
            bl = await conn.fetchrow(
                "SELECT slug, status, promoted_task_slug FROM tasks.backlog "
                "WHERE slug = $1 FOR UPDATE",
                slug,
            )
            if bl is None:
                raise HTTPException(status_code=404, detail=f"backlog '{slug}' does not exist")
            task_slug = bl["promoted_task_slug"]
            if not task_slug:
                # Item was not promoted (or was already reset). Return it to
                # 'open' as an idempotent no-op — the operator may be cleaning
                # up leftovers from a previous attempt that failed midway.
                await conn.execute(
                    """UPDATE tasks.backlog
                          SET status = 'open', promoted_task_slug = NULL, updated_at = now()
                        WHERE slug = $1""",
                    slug,
                )
                log.info("backlog_force_reset_noop", slug=slug, by=principal.username or "user")
                return {"ok": True, "backlog_slug": slug, "task_slug": None, "no_task": True}

            task = await conn.fetchrow(
                "SELECT id FROM tasks.tasks WHERE slug = $1 FOR UPDATE",
                task_slug,
            )

            topic_name = f"task-{task_slug}"
            # Covered convs: the task topic across all streams + subconvs whose
            # parent is the task conv (ask_agent creates __child-* with parent = root).
            await conn.execute(
                """UPDATE messaging.pending_asks pa
                      SET resolved_at = now()
                     WHERE pa.resolved_at IS NULL
                       AND pa.conversation_id IN (
                         SELECT c.id FROM messaging.conversations c
                          WHERE c.topic_name = $1
                             OR c.parent_conv_id IN (
                               SELECT c2.id FROM messaging.conversations c2
                                WHERE c2.topic_name = $1
                             )
                       )""",
                topic_name,
            )
            # CASCADE on messaging.conversations deletes child messages and
            # pending_asks; the ON DELETE CASCADE on parent_conv_id (migration 019)
            # removes ask_agent subconvs automatically.
            await conn.execute(
                "DELETE FROM messaging.conversations WHERE topic_name = $1",
                topic_name,
            )
            if task is not None:
                await conn.execute("DELETE FROM tasks.phases WHERE task_id = $1", task["id"])
                await conn.execute("DELETE FROM tasks.worktrees WHERE task_id = $1", task["id"])
                await conn.execute("DELETE FROM tasks.tasks WHERE id = $1", task["id"])
            await conn.execute(
                "DELETE FROM orchestrator.events WHERE task_slug = $1",
                task_slug,
            )
            await conn.execute(
                """UPDATE tasks.backlog
                      SET status = 'open', promoted_task_slug = NULL, updated_at = now()
                    WHERE slug = $1""",
                slug,
            )
    # Filesystem cleanup outside the DB transaction. Safety: validate the prefix.
    artifacts = _TASKS_ARTIFACT_DIR / task_slug
    fs_removed = False
    if artifacts.is_dir() and str(artifacts.resolve()).startswith(
        str(_TASKS_ARTIFACT_DIR.resolve()) + os.sep
    ):
        _shutil.rmtree(artifacts, ignore_errors=True)
        fs_removed = True
    log.info(
        "backlog_force_reset",
        slug=slug, task_slug=task_slug,
        fs_removed=fs_removed,
        by=principal.username or "user",
    )
    return {
        "ok": True,
        "backlog_slug": slug,
        "task_slug": task_slug,
        "fs_removed": fs_removed,
    }


@app.post("/api/backlog/{slug}/reopen")
async def backlog_reopen_endpoint(slug: str, principal: Principal = Depends(get_principal)):
    """Moves an item from `discarded` back to `open`. Unlike /revert,
    there is no task/conv/phases to clean up — discarding creates nothing.
    Used when the human changed their mind after discarding a backlog
    idea.

    Only accepts status='discarded'. Use /revert for promoted→open.
    """
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="invalid slug")
    row = await db.fetch_one(
        "SELECT status FROM tasks.backlog WHERE slug = $1",
        slug,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"backlog '{slug}' does not exist")
    if row["status"] != "discarded":
        raise HTTPException(
            status_code=409,
            detail=f"item is in '{row['status']}', not 'discarded' — use /revert for promoted",
        )
    await db.execute(
        "UPDATE tasks.backlog SET status = 'open', updated_at = now() WHERE slug = $1",
        slug,
    )
    log.info("backlog_reopened", slug=slug, by=principal.username or "user")
    return {"ok": True, "slug": slug, "status": "open"}


async def _resolve_task_conversation_ids(task_row: dict, phases: list[dict]) -> list[int]:
    """Collects conversation_ids of ALL conversations involved in a task:
      1. origin (where the human requested the task, first complete_phase).
      2. next_agent/next_topic of each orchestrator.event (one per handoff).
      3. terminal stream ($TERMINAL_NOTIFY_STREAM/task-<slug>-final) if configured.
      4. ask_agent sub-conversations (`__ask-from-<agent>-*`) during the task window.

    Used by /timeline and /stats. Refactored to avoid duplication.
    """
    slug = task_row["slug"]
    pairs: set[tuple[str, str]] = set()
    if task_row["origin_stream"] and task_row["origin_topic"]:
        pairs.add((task_row["origin_stream"], task_row["origin_topic"]))

    rows = await db.fetch_all(
        "SELECT payload FROM orchestrator.events WHERE task_slug = $1 ORDER BY id",
        slug,
    )
    for r in rows:
        p = r["payload"]
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except Exception:
                continue
        next_agent = p.get("next_agent")
        next_topic = p.get("next_topic")
        if next_agent and next_topic:
            pairs.add((next_agent, next_topic))

    terminal = os.environ.get("TERMINAL_NOTIFY_STREAM", "").strip()
    if terminal:
        pairs.add((terminal, f"task-{slug}-final"))

    agents_in_task = {p["agent"] for p in phases if p.get("agent")}
    if agents_in_task:
        started = [p["started_at"] for p in phases if p.get("started_at")]
        completed = [p["completed_at"] for p in phases if p.get("completed_at")]
        t_start = min(started) if started else None
        t_end = max(completed) if completed else (task_row["updated_at"] or t_start)
        if t_start and t_end:
            from datetime import timedelta as _td
            t_end_slack = t_end + _td(hours=1)
            conds = " OR ".join(
                f"(c.topic_name LIKE ${2*i+3} OR c.topic_name LIKE ${2*i+4})"
                for i in range(len(agents_in_task))
            )
            params: list = [t_start, t_end_slack]
            for a in agents_in_task:
                params.append(f"__ask-from-{a}-%")
                params.append(f"__ask-from-{a}.%")
            ask_rows = await db.fetch_all(
                f"""SELECT DISTINCT s.name AS stream, c.topic_name AS topic
                      FROM messaging.conversations c
                      JOIN messaging.streams s ON s.id = c.stream_id
                     WHERE ({conds})
                       AND c.last_message_at >= $1
                       AND c.last_message_at <= $2""",
                *params,
            )
            for r in ask_rows:
                pairs.add((r["stream"], r["topic"]))

    if not pairs:
        return []

    placeholders = ",".join(f"(${2*i+1}, ${2*i+2})" for i in range(len(pairs)))
    args: list = []
    for s, t in pairs:
        args.extend([s, t])
    convs = await db.fetch_all(
        f"""SELECT c.id
              FROM messaging.conversations c
              JOIN messaging.streams s ON s.id = c.stream_id
             WHERE (s.name, c.topic_name) IN ({placeholders})""",
        *args,
    )
    return [c["id"] for c in convs]


@app.get("/api/tasks/{slug}/stats")
async def task_stats(slug: str, _: Principal = Depends(get_principal)):
    """Aggregates cost/duration/turns/runs for the whole task, summing
    telemetry.events (event_type='run_end') across all involved
    conversations (same set used by /timeline).

    Small response: the PWA can call it together with /api/conversations
    without payload pain. The aggregate applies to threads with a task —
    free chat (no task) falls back to ConversationPanel's local calculation
    based on live events.
    """
    task_row = await _fetch_task_row(slug)
    if task_row is None:
        raise HTTPException(status_code=404, detail=f"task '{slug}' not found")
    phase_rows = await db.fetch_all(
        """SELECT agent, started_at, completed_at
             FROM tasks.phases WHERE task_id = $1 ORDER BY idx ASC""",
        task_row["id"],
    )
    phases = [dict(r) for r in phase_rows]
    conv_ids = await _resolve_task_conversation_ids(task_row, phases)

    # Primary source: telemetry.events via task_slug (migration 019). Survives
    # conv deletion. `turns` still comes from live_events because the summary
    # doesn't aggregate it — but it drops to 0 if all convs were deleted (trace gone).
    row = await db.fetch_one(
        """SELECT COUNT(*)                                   AS runs,
                  COALESCE(SUM(cost_usd), 0)::float          AS cost_usd,
                  COALESCE(SUM(duration_ms), 0)::bigint      AS duration_ms
             FROM telemetry.events
            WHERE task_slug = $1 AND event_type = 'run_end'""",
        slug,
    )
    turns = 0
    if conv_ids:
        turns_row = await db.fetch_one(
            """SELECT COALESCE(SUM((data->>'num_turns')::int), 0) AS turns
                 FROM telemetry.live_events
                WHERE conversation_id = ANY($1::int[]) AND kind = 'run_end'""",
            conv_ids,
        )
        turns = int(turns_row["turns"] or 0) if turns_row else 0
    return {
        "slug": slug,
        "runs": int(row["runs"] or 0),
        "cost_usd": float(row["cost_usd"] or 0),
        "duration_ms": int(row["duration_ms"] or 0),
        "turns": turns,
        "conversations": len(conv_ids),
    }


@app.get("/api/tasks/{slug}/telemetry")
async def task_telemetry(slug: str, _: Principal = Depends(get_principal)):
    """Detailed telemetry breakdown per task (plan item B4). Survives the
    deletion of the task's conversations (via task_slug in telemetry.events,
    migration 019). Returns totals + by_model + by_agent + the aggregated
    trace_snapshot of rows whose convs were already deleted."""
    task_row = await _fetch_task_row(slug)
    if task_row is None:
        raise HTTPException(status_code=404, detail=f"task '{slug}' not found")

    totals_row = await db.fetch_one(
        """SELECT COUNT(*)                                   AS runs,
                  COALESCE(SUM(cost_usd), 0)::float          AS cost_usd,
                  COALESCE(SUM(duration_ms), 0)::bigint      AS duration_ms,
                  COALESCE(SUM(input_tokens), 0)             AS input_tokens,
                  COALESCE(SUM(output_tokens), 0)            AS output_tokens,
                  COALESCE(SUM(cache_creation_tokens), 0) + COALESCE(SUM(cache_read_tokens), 0) AS cache_tokens,
                  MAX(EXTRACT(EPOCH FROM ts))::bigint        AS last_run_ts
             FROM telemetry.events
            WHERE task_slug = $1 AND event_type = 'run_end'""",
        slug,
    )
    by_model_rows = await db.fetch_all(
        """SELECT COALESCE(model, '(unknown)') AS model,
                  COUNT(*)                                   AS runs,
                  COALESCE(SUM(cost_usd), 0)::float          AS cost_usd,
                  COALESCE(AVG(duration_ms)::int, 0)         AS avg_duration_ms
             FROM telemetry.events
            WHERE task_slug = $1 AND event_type = 'run_end'
            GROUP BY COALESCE(model, '(unknown)') ORDER BY cost_usd DESC""",
        slug,
    )
    by_agent_rows = await db.fetch_all(
        """SELECT agent,
                  COUNT(*)                                   AS runs,
                  COALESCE(SUM(cost_usd), 0)::float          AS cost_usd,
                  COALESCE(AVG(duration_ms)::int, 0)         AS avg_duration_ms
             FROM telemetry.events
            WHERE task_slug = $1 AND event_type = 'run_end'
            GROUP BY agent ORDER BY cost_usd DESC""",
        slug,
    )
    # Pre-delete trace snapshots: we sum what is left in metadata.trace_snapshot
    # to show "effort" even after the convs are deleted.
    snap_row = await db.fetch_one(
        """SELECT COALESCE(SUM((metadata->'trace_snapshot'->>'thinking_count')::int), 0) AS thinking_count,
                  COALESCE(SUM((metadata->'trace_snapshot'->>'tool_use_count')::int), 0) AS tool_use_count,
                  COUNT(*) FILTER (WHERE metadata ? 'trace_snapshot')                    AS snapshots
             FROM telemetry.events
            WHERE task_slug = $1""",
        slug,
    )
    return {
        "slug": slug,
        "totals": dict(totals_row) if totals_row else {},
        "by_model": [dict(r) for r in by_model_rows],
        "by_agent": [dict(r) for r in by_agent_rows],
        "trace_snapshot": dict(snap_row) if snap_row else {},
    }


@app.get("/api/tasks/{slug}/timeline")
async def task_timeline(slug: str, _: Principal = Depends(get_principal)):
    """Unified timeline of a task: messages + live events from ALL involved
    conversations (origin + each handoff + terminal stream), sorted by
    timestamp. So the PWA can render everything in a single feed.
    """
    task_row = await _fetch_task_row(slug)
    if task_row is None:
        raise HTTPException(status_code=404, detail=f"task '{slug}' not found")
    # Load phases from the database (tasks schema).
    phase_rows = await db.fetch_all(
        """SELECT step, agent, started_at, completed_at, artifact, summary
             FROM tasks.phases WHERE task_id = $1 ORDER BY idx ASC""",
        task_row["id"],
    )
    phases = [dict(r) for r in phase_rows]
    meta = {
        "slug": task_row["slug"],
        "title": task_row["title"],
        "status": task_row["status"],
        "current_step": task_row["current_step"],
        "current_agent": task_row["current_agent"],
        "origin_stream": task_row["origin_stream"],
        "origin_topic":  task_row["origin_topic"],
        "workflow": task_row["workflow"],
        "updated_at": task_row["updated_at"].isoformat() if task_row["updated_at"] else None,
        "phases": [
            {
                "step": p["step"], "agent": p["agent"],
                "started_at":   p["started_at"].isoformat()   if p["started_at"]   else None,
                "completed_at": p["completed_at"].isoformat() if p["completed_at"] else None,
                "artifact": p["artifact"], "summary": p["summary"],
            } for p in phases
        ],
    }

    # Collect the involved (stream, topic) pairs:
    #   1. origin captured in the database (the coordinator's first complete_phase).
    #   2. next_agent/next_topic of each of the task's orchestrator.events.
    #   3. terminal stream (TERMINAL_NOTIFY_STREAM + task-<slug>-final) if configured.
    pairs: set[tuple[str, str]] = set()
    if task_row["origin_stream"] and task_row["origin_topic"]:
        pairs.add((task_row["origin_stream"], task_row["origin_topic"]))

    rows = await db.fetch_all(
        "SELECT payload FROM orchestrator.events WHERE task_slug = $1 ORDER BY id",
        slug,
    )
    for r in rows:
        p = r["payload"]
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except Exception:
                continue
        next_agent = p.get("next_agent")
        next_topic = p.get("next_topic")
        if next_agent and next_topic:
            pairs.add((next_agent, next_topic))

    terminal = os.environ.get("TERMINAL_NOTIFY_STREAM", "").strip()
    if terminal:
        pairs.add((terminal, f"task-{slug}-final"))

    # 4. ask_agent sub-conversations: each agent that TOOK PART in the task may
    #    have called ask_agent during its turn. Topic: `__ask-from-<chain>-<uid>`
    #    where the chain starts with the asking agent. Scope: conversations
    #    created during the task window (>= first event, <= last + 1h slack).
    from datetime import datetime as _dt
    agents_in_task = {p["agent"] for p in phases if p.get("agent")}

    if agents_in_task:
        started = [p["started_at"] for p in phases if p.get("started_at")]
        completed = [p["completed_at"] for p in phases if p.get("completed_at")]
        t_start = min(started) if started else None
        t_end = max(completed) if completed else (task_row["updated_at"] or t_start)
        if t_start and t_end:
            conds = " OR ".join(
                f"(c.topic_name LIKE ${2*i+3} OR c.topic_name LIKE ${2*i+4})"
                for i in range(len(agents_in_task))
            )
            from datetime import timedelta as _td
            t_end_slack = t_end + _td(hours=1)
            params: list = [t_start, t_end_slack]
            for a in agents_in_task:
                params.append(f"__ask-from-{a}-%")
                params.append(f"__ask-from-{a}.%")
            ask_rows = await db.fetch_all(
                f"""SELECT DISTINCT s.name AS stream, c.topic_name AS topic
                      FROM messaging.conversations c
                      JOIN messaging.streams s ON s.id = c.stream_id
                     WHERE ({conds})
                       AND c.last_message_at >= $1
                       AND c.last_message_at <= $2""",
                *params,
            )
            for r in ask_rows:
                pairs.add((r["stream"], r["topic"]))

    if not pairs:
        return {"slug": slug, "meta": meta, "conversations": [], "items": []}

    # Resolve conversation_ids.
    placeholders = ",".join(f"(${2*i+1}, ${2*i+2})" for i in range(len(pairs)))
    args: list = []
    for s, t in pairs:
        args.extend([s, t])
    convs = await db.fetch_all(
        f"""SELECT c.id, s.name AS stream, c.topic_name AS topic
              FROM messaging.conversations c
              JOIN messaging.streams s ON s.id = c.stream_id
             WHERE (s.name, c.topic_name) IN ({placeholders})""",
        *args,
    )
    conv_ids = [c["id"] for c in convs]
    convs_info = [{"id": c["id"], "stream": c["stream"], "topic": c["topic"]} for c in convs]

    if not conv_ids:
        return {"slug": slug, "meta": meta, "conversations": [], "items": []}

    # Fetch msgs + live_events across all of them.
    msgs = await db.fetch_all(
        """SELECT m.id, m.conversation_id, u.username AS sender, u.kind AS sender_kind,
                  m.content, EXTRACT(EPOCH FROM m.sent_at)::bigint AS ts,
                  s.name AS stream, c.topic_name AS topic
             FROM messaging.messages m
             JOIN messaging.users u ON u.id = m.sender_id
             JOIN messaging.conversations c ON c.id = m.conversation_id
             JOIN messaging.streams s ON s.id = c.stream_id
            WHERE m.conversation_id = ANY($1::int[])
            ORDER BY m.id""",
        conv_ids,
    )
    lev = await db.fetch_all(
        """SELECT id, conversation_id, agent, kind, summary, data,
                  EXTRACT(EPOCH FROM ts)::bigint AS ts
             FROM telemetry.live_events
            WHERE conversation_id = ANY($1::int[])
            ORDER BY id""",
        conv_ids,
    )

    items = []
    for m in msgs:
        items.append({
            "kind": "msg",
            "id": f"m{m['id']}",
            "ts": int(m["ts"]),
            "conversation_id": m["conversation_id"],
            "stream": m["stream"],
            "topic": m["topic"],
            "sender": m["sender"],
            "is_bot": m["sender_kind"] == "bot",
            "content": m["content"],
        })
    for e in lev:
        data = e["data"]
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                data = {}
        elif not isinstance(data, dict):
            data = {}
        items.append({
            "kind": "event",
            "id": f"e{e['id']}",
            "ts": int(e["ts"]),
            "conversation_id": e["conversation_id"],
            "agent": e["agent"],
            "event_kind": e["kind"],
            "summary": e["summary"],
            "data": data,
        })
    # Sort by ts asc; tie: msg before event.
    items.sort(key=lambda x: (x["ts"], 0 if x["kind"] == "msg" else 1))

    worktree_rows = await db.fetch_all(
        "SELECT repo, branch, path FROM tasks.worktrees WHERE task_id = $1",
        task_row["id"],
    )
    worktrees = {
        r["repo"]: {"repo": r["repo"], "branch": r["branch"], "path": r["path"]}
        for r in worktree_rows
    }
    return {
        "slug": slug,
        "meta": {
            "title": meta.get("title"),
            "status": meta.get("status"),
            "current_step": meta.get("current_step"),
            "current_agent": meta.get("current_agent"),
            "workflow": meta.get("workflow"),
            "phases": meta.get("phases") or [],
            "worktrees": worktrees,
        },
        "conversations": convs_info,
        "items": items,
    }


# ---------- Health / PWA static ----------

@app.get("/health")
async def health():
    ok = True
    try:
        await db.fetch_one("SELECT 1")
    except Exception:
        ok = False
    return {
        "status": "ok" if ok else "degraded",
        "db": ok,
        "vapid_enabled": app.state.push_dispatcher is not None,
        "transcriber_enabled": bool(os.environ.get("TRANSCRIBER_URL")),
    }


# SvelteKit build output (Phase 6 of big-bang). `static/` (sw.js, manifest,
# icons) and `build/` (SPA generated by Vite) are served side by side.
def _index_file() -> Path:
    return BUILD_DIR / "index.html"


# D-96 Cache strategy. Without this, FastAPI/Starlette sends no
# Cache-Control and the browser caches heuristically (10% of age) — in dev
# that makes the user see old bundles after a rebuild. Strategy:
#
#   /                       no-cache, must-revalidate  (entry HTML — small,
#                                                       references hashed
#                                                       bundles; must be
#                                                       fresh)
#   /sw.js                  no-cache                   (service worker —
#                                                       the browser already
#                                                       checks periodically,
#                                                       but ensures zero stale)
#   /manifest.webmanifest   no-cache                   (depends on .env, may
#                                                       change at runtime)
#   /manifest-icon/*.png    public, max-age=300        (icons rarely change,
#                                                       5min of cache is safe)
#   /_app/*                 public, max-age=31536000,  (bundles hashed by
#                           immutable                   Vite — new hash = new
#                                                       URL, content never
#                                                       changes at the same URL)
#   /static/*               public, max-age=3600       (fallback icons, sw source
#                                                       — fresh within 1h)
_NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}
_LONG_CACHE = {"Cache-Control": "public, max-age=300"}
_IMMUTABLE_CACHE = {"Cache-Control": "public, max-age=31536000, immutable"}


@app.get("/", include_in_schema=False)
async def index():
    f = _index_file()
    if not f.exists():
        return JSONResponse({"error": "frontend not installed"}, status_code=503)
    return FileResponse(f, media_type="text/html", headers=_NO_CACHE)


@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest():
    """D-94: manifest rendered dynamically from the env. Lets each instance
    configure name/color/etc without rebuilding the image. Generic defaults
    so it works out of the box."""
    body = {
        "id": os.environ.get("PWA_ID", "/ai-company"),
        "name": os.environ.get("PWA_NAME", "Agents"),
        "short_name": os.environ.get("PWA_SHORT_NAME", "Agents"),
        "description": os.environ.get(
            "PWA_DESCRIPTION", "Multi-agent orchestration"),
        "start_url": os.environ.get("PWA_START_URL", "/"),
        "scope": "/",
        "display": "standalone",
        "display_override": ["window-controls-overlay", "standalone", "minimal-ui"],
        "orientation": os.environ.get("PWA_ORIENTATION", "any"),
        "lang": os.environ.get("PWA_LANG", "en"),
        "categories": ["productivity"],
        "background_color": os.environ.get("PWA_BACKGROUND_COLOR", "#0b1220"),
        "theme_color": os.environ.get("PWA_THEME_COLOR", "#0b1220"),
        "launch_handler": {"client_mode": "focus-existing"},
        "icons": [
            {"src": "/manifest-icon/192.png", "sizes": "192x192",
             "type": "image/png", "purpose": "any"},
            {"src": "/manifest-icon/512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "any"},
        ],
    }
    return JSONResponse(
        body, media_type="application/manifest+json", headers=_NO_CACHE,
    )


@app.get("/manifest-icon/{size}.png", include_in_schema=False)
async def manifest_icon(size: int):
    """D-94: serves the PWA icon — prefers the per-instance override at
    `instance/web/icons/icon-<size>.png`, falls back to the framework default."""
    if size not in (192, 512):
        raise HTTPException(status_code=404, detail="invalid size")
    instance_path = INSTANCE_WEB_DIR / "icons" / f"icon-{size}.png"
    if instance_path.is_file():
        return FileResponse(instance_path, media_type="image/png", headers=_LONG_CACHE)
    return FileResponse(STATIC_DIR / f"icon-{size}.png", media_type="image/png", headers=_LONG_CACHE)


@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    return FileResponse(
        STATIC_DIR / "sw.js", media_type="application/javascript", headers=_NO_CACHE,
    )


# D-96: middleware sets Cache-Control on the /_app and /static mounts, which
# Starlette's StaticFiles serves without a cache header. The /_app/* bundle
# is hashed by Vite — content at the same URL never changes, so we can be
# aggressive (immutable, 1 year). /static/* holds the fallback icon and the
# sw source — 1h is safe.
@app.middleware("http")
async def _cache_control_static(request, call_next):
    response = await call_next(request)
    if "cache-control" in (k.lower() for k in response.headers.keys()):
        return response
    path = request.url.path
    if path.startswith("/_app/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=3600"
    return response


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
if (BUILD_DIR / "_app").is_dir():
    app.mount("/_app", StaticFiles(directory=str(BUILD_DIR / "_app")), name="svelte_app")


# ---------- Repos (init on behalf of agents) ----------

@app.post("/api/repos/init")
async def repos_init(payload: dict, principal: Principal = Depends(get_principal)):
    """Create a new git repo in the shared repos workspace. Idempotent.

    Agents can't do this themselves: they mount the workspace read-only
    (D-115). See app/repos.py for the layout."""
    if principal.kind != "bot" and not principal.is_admin:
        raise HTTPException(status_code=403, detail="only agents and admins can create repos")
    from . import repos as _repos
    try:
        result = await asyncio.to_thread(_repos.init_repo, payload.get("name") or "")
    except _repos.RepoError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        log.exception("repos.init_failed", name=payload.get("name"))
        raise HTTPException(status_code=500, detail=f"could not create repo: {e}")
    log.info("repos.init", by=principal.username, **result)
    return result


# ---------- TTS proxy ----------
#
# Talks to the ai-tts service (external/ai-tts). Contract notes:
# - `voice` must be omitted or null to use the default; "" is a 422.
# - Invalid input is a 422 whose `detail` is FastAPI's list of objects.
# - A voice that isn't installed is a 404; synthesis failures are a 500 with
#   a generic detail (the real error is in the tts container log).
# The proxy validates what it can up front and always answers the PWA with a
# string `detail`, whatever shape upstream used.

TTS_MAX_TEXT_CHARS = int(os.environ.get("TTS_MAX_TEXT_CHARS", "5000"))
_TTS_VOICE_RE = _re.compile(r"^[A-Za-z0-9_-]+$")
_TTS_MEDIA_TYPES = {"wav": "audio/wav", "mp3": "audio/mpeg"}


def _upstream_detail(body: str) -> str:
    """Extract a human-readable message from an upstream FastAPI error body.

    `detail` is a string for most errors and a list of
    `{"loc", "msg", "type"}` objects for validation errors (422)."""
    try:
        detail = json.loads(body).get("detail")
    except (ValueError, AttributeError):
        return body[:200]
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        msgs = [d.get("msg", "") for d in detail if isinstance(d, dict)]
        return "; ".join(m for m in msgs if m) or "invalid request"
    return body[:200]


@app.post("/api/tts/synthesize")
async def tts_synthesize(payload: dict, _: Principal = Depends(get_principal)):
    """Proxy to the internal TTS container (Piper). Returns audio/wav or audio/mpeg."""
    url = os.environ.get("TTS_URL")
    if not url:
        raise HTTPException(status_code=503, detail="TTS not configured")
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=422, detail="text is required")
    text = text.strip()
    if len(text) > TTS_MAX_TEXT_CHARS:
        raise HTTPException(status_code=422, detail=f"text is longer than {TTS_MAX_TEXT_CHARS} characters")
    body: dict = {"text": text}
    voice = payload.get("voice")
    if voice:  # "" / None -> omitted, so the service uses its default voice
        if not isinstance(voice, str) or not _TTS_VOICE_RE.match(voice):
            raise HTTPException(status_code=422, detail="voice may contain only letters, digits, '_' and '-'")
        body["voice"] = voice
    fmt = payload.get("format") or "wav"
    if fmt not in _TTS_MEDIA_TYPES:
        raise HTTPException(status_code=422, detail="format must be 'wav' or 'mp3'")
    body["format"] = fmt
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url.rstrip("/") + "/synthesize", json=body) as resp:
            if resp.status != 200:
                detail = _upstream_detail(await resp.text())
                if resp.status == 404:
                    raise HTTPException(status_code=404, detail=detail)
                if resp.status == 422:
                    raise HTTPException(status_code=422, detail=detail)
                log.warning("tts.upstream_error", status=resp.status, detail=detail)
                raise HTTPException(status_code=502, detail=f"TTS failed ({resp.status}); see the tts container log")
            data = await resp.read()
    return Response(content=data, media_type=_TTS_MEDIA_TYPES[fmt])


# ---------- Transcribe preview ----------
#
# Talks to the ai-transcriber native API (external/ai-transcriber):
# POST /transcribe, multipart `file` + optional `language`. Errors are
# `{"detail": ...}`; 503 means the model is loading or the queue is full.

TRANSCRIBER_MAX_UPLOAD_MB = int(os.environ.get("TRANSCRIBER_MAX_UPLOAD_MB", "50"))


@app.post("/api/transcribe-preview")
async def transcribe_preview(
    file: UploadFile = File(...),
    language: str | None = Form(default=None),
    _: Principal = Depends(get_principal),
):
    url = os.environ.get("TRANSCRIBER_URL")
    if not url:
        raise HTTPException(status_code=503, detail="Transcriber not configured")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(content) > TRANSCRIBER_MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"File is larger than {TRANSCRIBER_MAX_UPLOAD_MB}MB")
    form = aiohttp.FormData()
    form.add_field("file", content, filename=file.filename or "audio.webm",
                   content_type=file.content_type or "application/octet-stream")
    if language:
        form.add_field("language", language)
    headers = {}
    api_key = os.environ.get("TRANSCRIBER_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url.rstrip("/") + "/transcribe", data=form, headers=headers) as resp:
            body = await resp.text()
            if resp.status != 200:
                detail = _upstream_detail(body)
                if resp.status in (400, 413, 422):
                    raise HTTPException(status_code=resp.status, detail=detail)
                if resp.status == 503:
                    raise HTTPException(
                        status_code=503,
                        detail=f"Transcriber busy or loading the model: {detail}",
                        headers={"Retry-After": resp.headers.get("Retry-After", "5")},
                    )
                log.warning("transcriber.upstream_error", status=resp.status, detail=detail)
                raise HTTPException(status_code=502, detail=f"Transcriber failed ({resp.status}): {detail}")
            return json.loads(body)


# ---------- App Settings (web.app_settings) ----------
#
# Instance settings editable via PWA Settings -> System tab. Replaces
# (with transparent fallback) the WEB_DEFAULT_STREAM + VAPID_* env vars.
# Migration 031: web.app_settings.

@app.get("/api/web-settings/general")
async def web_settings_general(_: Principal = Depends(get_principal)):
    """Returns the config visible to the PWA. The VAPID private key NEVER
    leaves here — only the 'configured' flag + public_key + email."""
    from . import app_settings as _s
    default_stream = await _s.get_default_stream()
    vapid = await _s.get_vapid()
    return {
        "default_stream": default_stream,
        "vapid": {
            "configured": vapid is not None,
            "public_key": (vapid or {}).get("public_key", ""),
            "contact_email": (vapid or {}).get("contact_email", ""),
        },
    }


@app.put("/api/web-settings/general")
async def web_settings_update(payload: dict, principal: Principal = Depends(auth_mod.require_admin)):
    """Updates default_stream and/or vapid.contact_email. Does not touch
    the keypair — there is a dedicated /vapid/generate endpoint for that."""
    from . import app_settings as _s
    if "default_stream" in payload:
        await _s.set_default_stream(
            (payload.get("default_stream") or "").strip(),
            user_id=principal.user_id,
        )
    if "vapid_contact_email" in payload:
        email = (payload.get("vapid_contact_email") or "").strip()
        if email:
            await _s.set_vapid_contact_email(email, user_id=principal.user_id)
    return await web_settings_general(principal)  # type: ignore[arg-type]


@app.post("/api/web-settings/vapid/generate")
async def web_settings_vapid_generate(
    payload: dict | None = None,
    principal: Principal = Depends(auth_mod.require_admin),
):
    """Generates a new VAPID keypair and saves it in web.app_settings.

    Refuses by default if push subscriptions already exist (key rotation
    invalidates ALL existing subscriptions — clients must re-subscribe).
    The caller passes {"force": true} to confirm the destructive rotation.
    """
    from . import app_settings as _s
    payload = payload or {}
    force = bool(payload.get("force"))

    sub_count_row = await db.fetch_one(
        "SELECT COUNT(*)::int AS c FROM web.push_subscriptions"
    )
    sub_count = int((sub_count_row or {"c": 0})["c"])

    current = await _s.get_vapid()
    if current is not None and not force:
        raise HTTPException(
            status_code=409,
            detail=(
                f"VAPID already configured. Pass force=true to rotate "
                f"(will invalidate {sub_count} existing push subscription(s))."
            ),
        )

    new_kp = _s.generate_vapid_keypair()
    contact = (payload.get("contact_email") or (current or {}).get("contact_email") or "").strip()
    await _s.set_vapid(
        public_key=new_kp["public_key"],
        private_key=new_kp["private_key"],
        contact_email=contact or None,
        user_id=principal.user_id,
    )

    # Destructive rotation: clear old subscriptions — the client's next
    # subscribe will create a new record with the new key.
    invalidated = 0
    if force and sub_count:
        await db.execute("DELETE FROM web.push_subscriptions")
        invalidated = sub_count

    # Hot-reload the dispatcher so the new keypair takes effect without a restart.
    await _reload_push_dispatcher()

    log.info(
        "web_settings.vapid_generated",
        rotated=current is not None,
        invalidated=invalidated,
        by=principal.user_id,
    )
    return {
        "ok": True,
        "public_key": new_kp["public_key"],
        "subscriptions_invalidated": invalidated,
    }


@app.delete("/api/web-settings/vapid")
async def web_settings_vapid_clear(principal: Principal = Depends(auth_mod.require_admin)):
    """Removes the VAPID keypair + invalidates subscriptions. Push stays
    disabled until generate is called again."""
    from . import app_settings as _s
    await _s.clear_vapid()
    invalidated_row = await db.fetch_one(
        "SELECT COUNT(*)::int AS c FROM web.push_subscriptions"
    )
    invalidated = int((invalidated_row or {"c": 0})["c"])
    if invalidated:
        await db.execute("DELETE FROM web.push_subscriptions")
    await _reload_push_dispatcher()
    log.info(
        "web_settings.vapid_cleared",
        invalidated=invalidated,
        by=principal.user_id,
    )
    return {"ok": True, "subscriptions_invalidated": invalidated}


# ---------- Push ----------

@app.get("/api/push-config")
async def push_config():
    from . import app_settings as _s
    cfg = await _s.get_vapid()
    return {
        "enabled": cfg is not None,
        "public_key": (cfg or {}).get("public_key", ""),
    }


@app.post("/api/push/subscribe")
async def push_subscribe(payload: dict, principal: Principal = Depends(get_principal)):
    sub = payload.get("subscription") or payload
    endpoint = sub.get("endpoint")
    keys = sub.get("keys") or {}
    p256dh = keys.get("p256dh")
    auth_key = keys.get("auth")
    if not (endpoint and p256dh and auth_key):
        raise HTTPException(status_code=400, detail="invalid subscription")
    await db.execute(
        """INSERT INTO web.push_subscriptions (endpoint, p256dh, auth, user_id, user_agent)
           VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (endpoint) DO UPDATE SET
             p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth, user_agent = EXCLUDED.user_agent""",
        endpoint, p256dh, auth_key, principal.user_id, payload.get("user_agent"),
    )
    return {"ok": True}


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(payload: dict):
    endpoint = payload.get("endpoint")
    if not endpoint:
        raise HTTPException(status_code=400, detail="endpoint missing")
    await db.execute("DELETE FROM web.push_subscriptions WHERE endpoint = $1", endpoint)
    return {"ok": True}


@app.post("/api/push/test")
async def push_test(delay_seconds: float = 0.0):
    dispatcher = app.state.push_dispatcher
    if dispatcher is None:
        raise HTTPException(status_code=503, detail="VAPID not configured")

    async def _dispatch():
        subs = await db.fetch_all("SELECT endpoint, p256dh, auth FROM web.push_subscriptions")
        sent = 0
        for s in subs:
            try:
                await dispatcher.send_one(endpoint=s["endpoint"], p256dh=s["p256dh"], auth=s["auth"],
                                          title="Ping", body="Push test.", url="/", tag="test")
                sent += 1
            except Exception:
                log.exception("push.test_failed", endpoint=s["endpoint"][:40])
        return sent, len(subs)

    delay = max(0.0, min(60.0, float(delay_seconds)))
    if delay > 0:
        async def _delayed():
            try:
                await asyncio.sleep(delay)
                await _dispatch()
            except Exception:
                log.exception("push.test_delayed_failed")
        asyncio.create_task(_delayed())
        return {"ok": True, "scheduled_in": delay}

    sent, total = await _dispatch()
    return {"ok": True, "sent": sent, "total": total}


# ---------- Telemetry ----------

# ---------- Global search across messages ----------

@app.get("/api/search")
async def search_messages(q: str = "", limit: int = 30, _: Principal = Depends(get_principal)):
    """FTS over messaging.messages. Returns {items: [{stream, topic, conv_id,
    message_id, sender, snippet, ts}]} ordered by ts_rank desc."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"items": []}
    rows = await db.fetch_all(
        """
        SELECT m.id              AS message_id,
               s.name            AS stream,
               c.topic_name      AS topic,
               c.id              AS conv_id,
               u.username        AS sender,
               m.sent_at         AS ts,
               ts_headline(
                   'simple', m.content,
                   plainto_tsquery('simple', $1),
                   'StartSel=<mark>, StopSel=</mark>, MaxFragments=2, MaxWords=20, MinWords=5'
               )                 AS snippet,
               ts_rank(to_tsvector('simple', m.content),
                       plainto_tsquery('simple', $1)) AS rank
          FROM messaging.messages m
          JOIN messaging.conversations c ON c.id = m.conversation_id
          JOIN messaging.streams s       ON s.id = c.stream_id
          JOIN messaging.users u         ON u.id = m.sender_id
         WHERE to_tsvector('simple', m.content) @@ plainto_tsquery('simple', $1)
         ORDER BY rank DESC, m.sent_at DESC
         LIMIT $2
        """,
        q, min(int(limit), 100),
    )
    return {
        "items": [
            {
                "message_id": r["message_id"],
                "stream": r["stream"],
                "topic": r["topic"],
                "conv_id": f"{r['stream']}/{r['topic']}",
                "sender": r["sender"],
                "snippet": r["snippet"],
                "ts": r["ts"].timestamp(),
            } for r in rows
        ]
    }


# ---------- Live trace ----------
# Agents post claude stream-json events (run_start/thinking/tool_use/
# tool_result/run_end) per conversation. The PWA subscribes via SSE to see
# activity in real time. Cleanup via scheduler (cleanup_live_events).

@app.post("/api/telemetry/live-event")
async def telemetry_live_event(payload: dict, principal: Principal = Depends(get_principal)):
    """Accepts {stream, topic, kind, summary?, data?, agent?}. Resolves conv_id
    via stream+topic; the INSERT fires pg_notify for SSE.

    Dual-write (D-NN, migration 030): keeps messaging.runs in sync with the
    live_events stream in the same transaction:
      * run_start  → INSERT row with status='running' (ON CONFLICT bumps the
                     heartbeat if a running row already exists for the conv).
      * run_end    → UPDATE the running row to done/error depending on subtype.
      * anything   → bumps last_heartbeat_at on the running row (piggyback
                     heartbeat, fine granularity without a separate timer).
    If the runs part fails, the rollback takes the live_event with it — that
    is acceptable: the runner's fire-and-forget re-emits or the reaper compensates.
    """
    stream = payload.get("stream")
    topic = payload.get("topic")
    kind = payload.get("kind")
    if not (stream and topic and kind):
        raise HTTPException(status_code=400, detail="stream, topic, kind are required")
    # thinking can be multi-paragraph; a high but real cap avoids an absurd
    # payload coming from the runner. The UI renders the full text as a bubble.
    summary = payload.get("summary")
    if summary and len(summary) > 8000:
        summary = summary[:7997] + "..."
    # seq_num (migration 018): generated in the agent for a deterministic
    # tie-break when 2 events share a ts. Optional — old clients and
    # synthetic events from mcp.server.py don't send it; reads fall back to
    # the id via COALESCE.
    seq_num = payload.get("seq_num")
    if seq_num is not None:
        try:
            seq_num = int(seq_num)
        except (TypeError, ValueError):
            seq_num = None
    data = payload.get("data") or {}
    agent = payload.get("agent", principal.username)
    async with db.connection() as conn:
        row = await conn.fetchrow(
            """SELECT c.id FROM messaging.conversations c
                 JOIN messaging.streams s ON s.id = c.stream_id
                WHERE s.name = $1 AND c.topic_name = $2""",
            stream, topic,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="conv does not exist")
        conv_id = row["id"]
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO telemetry.live_events
                   (conversation_id, agent, kind, summary, data, seq_num)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                conv_id, agent, kind, summary, json.dumps(data), seq_num,
            )
            await _runs_apply_event(conn, conv_id, agent, stream, topic, kind, data)
    return {"ok": True}


async def _runs_apply_event(
    conn, conv_id: int, agent: str, stream: str, topic: str,
    kind: str, data: dict,
) -> None:
    """Keeps messaging.runs in sync with the event. Called inside the
    telemetry_live_event handler's txn."""
    if kind == "run_start":
        # Idempotent: if there is already a 'running' row for this conv
        # (runner re-emit or duplicate-POST jitter), only bump the heartbeat.
        # Normal case: create a new 'running' row. The reaper moves it to
        # 'stale' if the previous run died without run_end — when that
        # happens, the INSERT below doesn't hit the partial unique index
        # (which only covers status='running') and creates a new row as expected.
        await conn.execute(
            """INSERT INTO messaging.runs
                 (conversation_id, agent, topic_slug, status, metadata)
               VALUES ($1, $2, $3, 'running', $4::jsonb)
               ON CONFLICT (conversation_id) WHERE status = 'running'
               DO UPDATE SET last_heartbeat_at = now()""",
            conv_id, agent, topic,
            json.dumps({"start_summary": data} if data else {}),
        )
    elif kind == "run_end":
        # Claude CLI convention: subtype None or 'success' = ok; otherwise error.
        # The synthetic run_end (D-71, claude_runner.py:1290) also sends a
        # subtype; we treat it the same.
        subtype = data.get("subtype") if isinstance(data, dict) else None
        new_status = "done" if subtype in (None, "success") else "error"
        # Update the most recent running row for this conv. Subquery by
        # PK so the UPDATE can use ORDER BY/LIMIT.
        await conn.execute(
            """UPDATE messaging.runs
                  SET status = $2,
                      finished_at = now(),
                      exit_reason = $3,
                      last_heartbeat_at = now()
                WHERE id = (
                    SELECT id FROM messaging.runs
                     WHERE conversation_id = $1 AND status = 'running'
                     ORDER BY started_at DESC LIMIT 1
                )""",
            conv_id, new_status, subtype,
        )
    else:
        # thinking / tool_use / tool_result / etc → heartbeat free.
        # No-op if there is no 'running' row (defensive; can happen if
        # run_start was lost in the fire-and-forget).
        await conn.execute(
            """UPDATE messaging.runs
                  SET last_heartbeat_at = now()
                WHERE conversation_id = $1 AND status = 'running'""",
            conv_id,
        )


@app.get("/api/conversations/{conv_id:path}/live/recent")
async def conversation_live_recent(conv_id: str, limit: int = 50, _: Principal = Depends(get_principal)):
    numeric_id = await _resolve_conv_id(conv_id)
    rows = await db.fetch_all(
        """SELECT id, agent, kind, summary, ts, data,
                  COALESCE(seq_num, id) AS seq_num
             FROM telemetry.live_events
            WHERE conversation_id = $1
            ORDER BY id DESC
            LIMIT $2""",
        numeric_id, min(int(limit), 500),
    )
    items = []
    for r in reversed(rows):
        data = r["data"]
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                data = {}
        elif not isinstance(data, dict):
            data = {}
        items.append({
            "id": r["id"], "agent": r["agent"], "kind": r["kind"],
            "summary": r["summary"], "ts": r["ts"].timestamp(),
            "data": data, "seq_num": r["seq_num"],
        })
    return {"items": items}


@app.get("/api/conversations/{conv_id:path}/live/stream")
async def conversation_live_stream(conv_id: str, _: Principal = Depends(get_principal)):
    """SSE LISTEN on the `live_event_<conv_id>` channel. The connection stays
    open until the client disconnects. Heartbeat every 25s to avoid idle close."""
    numeric_id = await _resolve_conv_id(conv_id)
    channel = f"live_event_{numeric_id}"
    dsn = os.environ["DATABASE_URL"]

    async def event_generator():
        conn = await asyncpg.connect(dsn)
        queue: asyncio.Queue[str] = asyncio.Queue()

        def _cb(_c, _pid, _ch, payload):
            queue.put_nowait(payload)

        await conn.add_listener(channel, _cb)
        # Signal ready to the client
        yield f": connected to {channel}\n\n"
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=25.0)
                    # The trigger only sends id/agent/ts/kind/summary (fits the
                    # 8KB pg_notify limit). The frontend needs `data` (tool name,
                    # input) to render tool_use without the '?' fallback. Hydrate
                    # via lookup; +1 query per event, but live-event SSE is
                    # low frequency.
                    try:
                        evt = json.loads(payload)
                        row = await conn.fetchrow(
                            "SELECT data, COALESCE(seq_num, id) AS seq_num "
                            "FROM telemetry.live_events WHERE id = $1",
                            evt.get("id"),
                        )
                        if row is not None:
                            data = row["data"]
                            if isinstance(data, str):
                                try:
                                    data = json.loads(data)
                                except Exception:
                                    data = {}
                            elif not isinstance(data, dict):
                                data = {}
                            evt["data"] = data
                            evt["seq_num"] = row["seq_num"]
                            payload = json.dumps(evt)
                    except Exception:
                        # Fallback: send the trigger's raw payload if hydration
                        # fails (row gone, invalid JSON, etc).
                        pass
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    # Heartbeat-comment SSE
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await conn.remove_listener(channel, _cb)
                await conn.close()
            except Exception:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/live_events/{event_id}/full")
async def live_event_full(event_id: int, _: Principal = Depends(get_principal)):
    """Returns the full `data` of a live_event (including `output_full`,
    which the trigger strips from the NOTIFY to fit in 8KB). Used by the PWA
    when the user clicks "View full" on a truncated tool_result.
    JSONB in the database has no practical cap — the real limit is
    claude_runner's OUTPUT_FULL_CAP (~200KB)."""
    row = await db.fetch_one(
        "SELECT id, agent, kind, summary, ts, data FROM telemetry.live_events WHERE id = $1",
        event_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="live_event does not exist")
    data = row["data"]
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    elif not isinstance(data, dict):
        data = {}
    return {
        "id": row["id"],
        "agent": row["agent"],
        "kind": row["kind"],
        "summary": row["summary"],
        "ts": row["ts"].timestamp() if row["ts"] else None,
        "data": data,
    }


@app.get("/api/tasks/events")
async def tasks_events_stream(_: Principal = Depends(get_principal)):
    """SSE LISTEN on the `task_changed` channel (trigger `tasks.tasks` AFTER
    INSERT/UPDATE, migration 012). Slim payload: slug+status+current_agent.
    The PWA consumer refetches the list — derived fields (phases_count etc)
    come from the existing REST endpoint."""
    dsn = os.environ["DATABASE_URL"]

    async def event_generator():
        conn = await asyncpg.connect(dsn)
        queue: asyncio.Queue[str] = asyncio.Queue()

        def _cb(_c, _pid, _ch, payload):
            queue.put_nowait(payload)

        await conn.add_listener("task_changed", _cb)
        yield ": connected to task_changed\n\n"
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await conn.remove_listener("task_changed", _cb)
                await conn.close()
            except Exception:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/scheduler/events")
async def scheduler_events_stream(_: Principal = Depends(get_principal)):
    """SSE LISTEN on the `scheduler_event` channel. Emitted by the scheduler
    container via `_pg_notify` (orchestrator/scheduler.py) at the end of each
    dispatch_job — the payload has job_id, action, status, last_fire_at."""
    dsn = os.environ["DATABASE_URL"]

    async def event_generator():
        conn = await asyncpg.connect(dsn)
        queue: asyncio.Queue[str] = asyncio.Queue()

        def _cb(_c, _pid, _ch, payload):
            queue.put_nowait(payload)

        await conn.add_listener("scheduler_event", _cb)
        yield ": connected to scheduler_event\n\n"
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await conn.remove_listener("scheduler_event", _cb)
                await conn.close()
            except Exception:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/telemetry/event")
async def telemetry_event(payload: dict, principal: Principal = Depends(get_principal)):
    # task_slug: the runner sends it explicitly. Server-side fallback for
    # telemetry from old clients: topic_slug comes as '<stream>__<topic>',
    # part 2 is usually the topic_name; if it starts with 'task-', extract the slug.
    task_slug = payload.get("task_slug")
    if not task_slug:
        topic = payload.get("topic_slug") or ""
        topic_name = topic.split("__", 1)[1] if "__" in topic else topic
        if topic_name.startswith("task-"):
            task_slug = topic_name[5:]
    # conversation_id: the new runner sends stream+topic; resolve the same
    # way as /live-event. Best-effort — if the conv no longer exists (e.g.
    # a run emits telemetry after a delete), it stays NULL. Also accepts an
    # explicit conversation_id in the payload (tests/migration backfill).
    conversation_id = payload.get("conversation_id")
    if conversation_id is None:
        stream = payload.get("stream")
        topic_name = payload.get("topic")
        if stream and topic_name:
            row = await db.fetch_one(
                """SELECT c.id FROM messaging.conversations c
                     JOIN messaging.streams s ON s.id = c.stream_id
                    WHERE s.name = $1 AND c.topic_name = $2""",
                stream, topic_name,
            )
            if row is not None:
                conversation_id = row["id"]
    # Final task_slug fallback when the topic has no `task-` prefix
    # (the single mega-agent case: it operates in the human's original topic,
    # e.g. `2026-05-05 15:16`, no prefix). If conversation_id resolved,
    # join with tasks.tasks on origin_stream/origin_topic — take the most
    # recent task with that origin. For the classic multi-agent setup this
    # branch doesn't run because the `task-` prefix already resolved task_slug
    # above. Edge: if several tasks share the same origin (the same conv
    # ran X then Y), attribute to Y (most recent) — the desired behavior
    # ("task currently active in this conv").
    if not task_slug and conversation_id is not None:
        row = await db.fetch_one(
            """SELECT t.slug
                 FROM tasks.tasks t
                 JOIN messaging.streams s ON s.name = t.origin_stream
                 JOIN messaging.conversations c
                   ON c.stream_id = s.id AND c.topic_name = t.origin_topic
                WHERE c.id = $1
                ORDER BY t.updated_at DESC
                LIMIT 1""",
            conversation_id,
        )
        if row is not None:
            task_slug = row["slug"]
    await db.execute(
        """INSERT INTO telemetry.events
           (agent, topic_slug, event_type, cost_usd,
            input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
            duration_ms, model, metadata, conversation_id, task_slug)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)""",
        payload.get("agent", principal.username),
        payload.get("topic_slug"),
        payload.get("event_type", "unknown"),
        payload.get("cost_usd"),
        payload.get("input_tokens"),
        payload.get("output_tokens"),
        payload.get("cache_creation_tokens"),
        payload.get("cache_read_tokens"),
        payload.get("duration_ms"),
        payload.get("model"),
        json.dumps(payload.get("metadata", {})),
        conversation_id,
        task_slug,
    )
    return {"ok": True}


@app.get("/api/telemetry/summary")
async def telemetry_summary(
    window: str = "24h",
    agent: str | None = None,
    model: str | None = None,
):
    window_map = {"1h": "1 hour", "24h": "24 hours", "7d": "7 days", "30d": "30 days"}
    interval = window_map.get(window, "24 hours")
    # The 3 input counters are disjoint (Anthropic semantics): input_tokens
    # = uncached, cache_creation = written to cache, cache_read = read from cache.
    # Each has its own price. We expose the 3 summed over the window + a derived
    # `cache_tokens` (creation+read) so the breakdown stays easy in the PWA.
    filters = [f"ts > now() - INTERVAL '{interval}'"]
    params: list = []
    if agent:
        params.append(agent)
        filters.append(f"agent = ${len(params)}")
    if model:
        params.append(model)
        filters.append(f"model = ${len(params)}")
    where_sql = " AND ".join(filters)

    by_agent_rows = await db.fetch_all(
        f"""SELECT agent,
               COUNT(*) AS runs,
               COALESCE(SUM(cost_usd), 0)::float AS cost_usd,
               COALESCE(AVG(duration_ms)::int, 0) AS avg_duration_ms,
               COALESCE(SUM(input_tokens), 0) AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COALESCE(SUM(cache_creation_tokens), 0) + COALESCE(SUM(cache_read_tokens), 0) AS cache_tokens,
               COUNT(*) FILTER (WHERE event_type LIKE '%error%') AS failures
             FROM telemetry.events
            WHERE {where_sql}
            GROUP BY agent ORDER BY cost_usd DESC""",
        *params,
    )
    by_model_rows = await db.fetch_all(
        f"""SELECT COALESCE(model, '(unknown)') AS model,
               COUNT(*) AS runs,
               COALESCE(SUM(cost_usd), 0)::float AS cost_usd,
               COALESCE(AVG(duration_ms)::int, 0) AS avg_duration_ms,
               COALESCE(SUM(input_tokens), 0) AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COALESCE(SUM(cache_creation_tokens), 0) + COALESCE(SUM(cache_read_tokens), 0) AS cache_tokens,
               COUNT(*) FILTER (WHERE event_type LIKE '%error%') AS failures
             FROM telemetry.events
            WHERE {where_sql}
            GROUP BY COALESCE(model, '(unknown)') ORDER BY cost_usd DESC""",
        *params,
    )
    totals_row = await db.fetch_one(
        f"""SELECT COUNT(*) AS runs,
                   COALESCE(SUM(cost_usd), 0)::float AS total_cost_usd,
                   COALESCE(SUM(duration_ms), 0) AS total_duration_ms,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(cache_creation_tokens), 0) + COALESCE(SUM(cache_read_tokens), 0) AS cache_tokens
              FROM telemetry.events WHERE {where_sql}""",
        *params,
    )
    return {
        "window": window,
        "filters": {"agent": agent, "model": model},
        "totals": dict(totals_row) if totals_row else {},
        "by_agent": [dict(r) for r in by_agent_rows],
        "by_model": [dict(r) for r in by_model_rows],
    }


@app.get("/api/telemetry/timeseries")
async def telemetry_timeseries(
    window: str = "24h",
    agent: str | None = None,
    model: str | None = None,
):
    """Aggregated series for the time-series chart. Bucket size is automatic."""
    cfg = {
        "1h":  ("1 hour",   "5 minutes"),
        "24h": ("24 hours", "1 hour"),
        "7d":  ("7 days",   "6 hours"),
        "30d": ("30 days",  "1 day"),
    }
    interval, bucket = cfg.get(window, cfg["24h"])
    filters = [f"ts > now() - INTERVAL '{interval}'"]
    params: list = []
    if agent:
        params.append(agent)
        filters.append(f"agent = ${len(params)}")
    if model:
        params.append(model)
        filters.append(f"model = ${len(params)}")
    where_sql = " AND ".join(filters)
    rows = await db.fetch_all(
        f"""SELECT date_bin('{bucket}', ts, TIMESTAMP '2000-01-01')::timestamptz AS bucket,
                   COUNT(*) AS runs,
                   COALESCE(SUM(cost_usd), 0)::float AS cost_usd,
                   COALESCE(SUM(duration_ms), 0)::bigint AS duration_ms,
                   COALESCE(SUM(output_tokens), 0)::bigint AS output_tokens
              FROM telemetry.events
             WHERE {where_sql}
             GROUP BY bucket
             ORDER BY bucket""",
        *params,
    )
    return {
        "window": window,
        "bucket": bucket,
        "points": [
            {
                "ts": int(r["bucket"].timestamp()),
                "runs": int(r["runs"]),
                "cost_usd": float(r["cost_usd"] or 0),
                "duration_ms": int(r["duration_ms"] or 0),
                "output_tokens": int(r["output_tokens"] or 0),
            } for r in rows
        ],
    }


@app.get("/api/telemetry/recent")
async def telemetry_recent(
    limit: int = 50,
    agent: str | None = None,
    model: str | None = None,
):
    filters: list[str] = []
    params: list = []
    if agent:
        params.append(agent)
        filters.append(f"agent = ${len(params)}")
    if model:
        params.append(model)
        filters.append(f"model = ${len(params)}")
    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    params.append(min(limit, 500))
    rows = await db.fetch_all(
        f"""SELECT * FROM telemetry.events {where_sql}
             ORDER BY ts DESC LIMIT ${len(params)}""",
        *params,
    )
    import json as _json
    items = []
    for r in rows:
        meta = r["metadata"]
        if isinstance(meta, str):
            try:
                meta = _json.loads(meta)
            except Exception:
                meta = {}
        elif not isinstance(meta, dict):
            meta = {}
        items.append({
            "id": r["id"], "agent": r["agent"], "topic_slug": r["topic_slug"],
            "event_type": r["event_type"],
            "cost_usd": float(r["cost_usd"] or 0),
            "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
            "duration_ms": r["duration_ms"], "model": r["model"],
            "started_at": int(r["ts"].timestamp()),
            "ts": r["ts"].isoformat(), "metadata": meta,
            "num_turns": meta.get("num_turns"),
            "exit_code": meta.get("exit_code"),
        })
    return {"items": items}


# ---------- Company context + philosophy + agent policies ----------

COMPANY_CONTEXT_PATH = Path("/workspace/company/CONTEXT.md")
COMPANY_PHILOSOPHY_PATH = Path("/workspace/company/philosophy.md")
COMPANY_FILE_MAX_BYTES = 64 * 1024  # 64KB cap each (the system prompt trims later)


def _read_company_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except Exception as e:
        log.exception("company.read_failed", path=str(path), err=str(e))
        return ""


def _write_company_file(path: Path, content: str) -> int:
    if len(content.encode("utf-8")) > COMPANY_FILE_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"content > {COMPANY_FILE_MAX_BYTES} bytes")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path.stat().st_size


@app.get("/api/company/context")
async def company_context_get(_: Principal = Depends(get_principal)):
    return {"path": "company/CONTEXT.md", "content": _read_company_file(COMPANY_CONTEXT_PATH)}


@app.post("/api/company/context")
async def company_context_set(payload: dict, _: Principal = Depends(get_principal)):
    content = payload.get("content", "")
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="content must be a string")
    size = _write_company_file(COMPANY_CONTEXT_PATH, content)
    return {"ok": True, "size": size}


@app.get("/api/company/philosophy")
async def company_philosophy_get(_: Principal = Depends(get_principal)):
    return {"path": "company/philosophy.md", "content": _read_company_file(COMPANY_PHILOSOPHY_PATH)}


@app.post("/api/company/philosophy")
async def company_philosophy_set(payload: dict, _: Principal = Depends(get_principal)):
    content = payload.get("content", "")
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="content must be a string")
    size = _write_company_file(COMPANY_PHILOSOPHY_PATH, content)
    return {"ok": True, "size": size}


PHILOSOPHIES_TEMPLATES_DIR = Path("/app/templates/philosophies")


@app.get("/api/philosophies/templates")
async def list_philosophy_templates(_: Principal = Depends(get_principal)):
    """Lists the available philosophy templates (framework/templates/philosophies/)."""
    if not PHILOSOPHIES_TEMPLATES_DIR.is_dir():
        return {"items": []}
    items = []
    for f in sorted(PHILOSOPHIES_TEMPLATES_DIR.glob("*.md")):
        if f.name.startswith("_"):
            continue
        try:
            text = f.read_text(encoding="utf-8")
            # extract the title from the 1st line "# X" and the summary from the block
            lines = text.splitlines()
            title = lines[0].lstrip("# ").strip() if lines else f.stem
            summary = ""
            for line in lines[:5]:
                if "summary:" in line.lower():
                    summary = line.split("summary:", 1)[1].strip(" *_")
                    break
            items.append({
                "slug": f.stem,
                "title": title,
                "summary": summary,
                "content": text,
            })
        except Exception:
            log.exception("philosophy_template.read_failed", file=str(f))
    return {"items": items}


@app.get("/api/agents")
async def list_agents_meta(_: Principal = Depends(get_principal)):
    """Lists active agents (kind=bot, is_active=true) with metadata for the
    policies/onboard UI. Bots deactivated via reconcile/soft-delete (agent
    removed from agents.yaml) are left out — old convs stay accessible but
    the agent no longer shows up as a callable peer."""
    rows = await db.fetch_all(
        """SELECT u.username, u.full_name, u.agent_name
             FROM messaging.users u
            WHERE u.kind = 'bot' AND u.agent_name IS NOT NULL
              AND u.is_active = true
            ORDER BY u.agent_name"""
    )
    return {
        "items": [
            {
                "name": r["agent_name"],
                "display_name": r["full_name"],
                "username": r["username"],
            } for r in rows
        ]
    }


@app.get("/api/agent-policies")
async def list_agent_policies(_: Principal = Depends(get_principal)):
    rows = await db.fetch_all(
        "SELECT agent, can_ask, can_be_asked_by, updated_at FROM messaging.agent_policies ORDER BY agent"
    )
    return {
        "items": [
            {
                "agent": r["agent"],
                "can_ask": r["can_ask"],
                "can_be_asked_by": r["can_be_asked_by"],
                "updated_at": r["updated_at"].isoformat(),
            } for r in rows
        ]
    }


@app.post("/api/agent-policies")
async def upsert_agent_policy(payload: dict, _: Principal = Depends(get_principal)):
    """Upsert policy. Body: {agent, can_ask?, can_be_asked_by?}.
    can_ask=null OR missing => no restriction (may ask anyone).
    can_ask=[] => blocked from asking anyone.
    can_ask=['x','y'] => whitelist."""
    agent = (payload.get("agent") or "").strip()
    if not agent:
        raise HTTPException(status_code=400, detail="agent is required")

    def _norm(v):
        if v is None:
            return None
        if not isinstance(v, list):
            raise HTTPException(status_code=400, detail="can_ask/can_be_asked_by must be lists or null")
        return [str(x).strip() for x in v if str(x).strip()]

    can_ask = _norm(payload.get("can_ask"))
    can_be_asked_by = _norm(payload.get("can_be_asked_by"))
    await db.execute(
        """INSERT INTO messaging.agent_policies (agent, can_ask, can_be_asked_by)
           VALUES ($1, $2, $3)
           ON CONFLICT (agent) DO UPDATE SET
             can_ask = EXCLUDED.can_ask,
             can_be_asked_by = EXCLUDED.can_be_asked_by,
             updated_at = now()""",
        agent, can_ask, can_be_asked_by,
    )
    return {"ok": True, "agent": agent}


@app.delete("/api/agent-policies/{agent}")
async def delete_agent_policy(agent: str, _: Principal = Depends(get_principal)):
    await db.execute("DELETE FROM messaging.agent_policies WHERE agent = $1", agent)
    return {"ok": True}


# ---------- System prompts (D-63) ----------
# Everything that goes into `claude -p`'s --append-system-prompt lives here —
# editable via the PWA, read on every invocation by claude_runner (no restart).

import yaml  # noqa: E402

SYSTEM_PROMPTS_DIR = Path("/workspace/company/system_prompts")
# platform.md is framework-fixed (read-only); it lives in the image, not the
# instance volume. Mirrors claude_runner._PLATFORM_PROMPT_PATH.
PLATFORM_PROMPT_PATH = Path("/app/system_prompts/platform.md")
SYSTEM_PROMPTS_CONFIG_PATH = SYSTEM_PROMPTS_DIR / "config.yaml"
# In the web container, AGENTS_DIR is bind-mounted at /workspace/agents (vs /app/agents
# in the agent containers — different paths per container, on purpose).
AGENTS_CONTAINER_DIR = Path("/workspace/agents")

SYSTEM_PROMPT_SECTION_FILE = 64 * 1024  # per-file cap (same as CONTEXT)

# Defaults applied if a key is missing from config.yaml — mirrors claude_runner.
SYSTEM_PROMPT_TOGGLE_DEFAULTS: dict[str, bool] = {
    "include_platform_prompt": True,
    "include_company_context": True,
    "include_company_philosophy": True,
    "include_agent_claude_md": True,
    "include_team_block": True,
    # Contextual dynamic blocks (D-110+):
    "include_invocation_context": True,
    "include_task_state": True,
    "include_step_instructions": True,
}


def _read_system_prompt_config() -> dict[str, bool]:
    toggles = dict(SYSTEM_PROMPT_TOGGLE_DEFAULTS)
    try:
        raw = SYSTEM_PROMPTS_CONFIG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return toggles
    except Exception:
        log.exception("system_prompt.config_read_failed")
        return toggles
    try:
        data = yaml.safe_load(raw) or {}
    except Exception:
        log.exception("system_prompt.config_parse_failed")
        return toggles
    if isinstance(data, dict):
        for key in toggles:
            v = data.get(key)
            if isinstance(v, bool):
                toggles[key] = v
    return toggles


def _write_system_prompt_config(toggles: dict[str, bool]) -> None:
    SYSTEM_PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    header = (
        "# system_prompts/config.yaml — global system prompt toggles.\n"
        "# Edited by the PWA (Company > System Prompts). claude_runner re-reads it\n"
        "# on every invocation — changes apply to the next task, no restart.\n\n"
    )
    body = yaml.safe_dump(toggles, default_flow_style=False, sort_keys=False)
    SYSTEM_PROMPTS_CONFIG_PATH.write_text(header + body, encoding="utf-8")


def _agent_claude_md_path(agent: str) -> Path:
    # Validate the name (same regex as reconcile NAME_RE) to avoid path traversal.
    import re
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", agent):
        raise HTTPException(status_code=400, detail=f"invalid agent name: {agent!r}")
    return AGENTS_CONTAINER_DIR / agent / "CLAUDE.md"


def _read_section(key: str) -> tuple[Path, str]:
    """Resolves a section key to (path, content). Supports `agent:<name>`."""
    if key == "platform":
        path = PLATFORM_PROMPT_PATH
    elif key == "context":
        path = COMPANY_CONTEXT_PATH
    elif key == "philosophy":
        path = COMPANY_PHILOSOPHY_PATH
    elif key.startswith("agent:"):
        path = _agent_claude_md_path(key.split(":", 1)[1])
    else:
        raise HTTPException(status_code=404, detail=f"unknown section: {key!r}")
    try:
        return path, path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return path, ""
    except Exception as e:
        log.exception("system_prompt.section_read_failed", section=key)
        raise HTTPException(status_code=500, detail=f"read failed: {e}")


def _write_section(key: str, content: str) -> int:
    if key == "platform":
        # Framework invariant — shipped in the agent/web images, not editable
        # per-instance. The PWA hides the editor; this guard is defense-in-depth.
        raise HTTPException(
            status_code=403,
            detail="platform section is framework-fixed and not editable",
        )
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="content must be a string")
    if len(content.encode("utf-8")) > SYSTEM_PROMPT_SECTION_FILE:
        raise HTTPException(
            status_code=413,
            detail=f"content > {SYSTEM_PROMPT_SECTION_FILE} bytes",
        )
    path, _ = _read_section(key)  # validates key (and checks traversal for agent:)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path.stat().st_size


@app.get("/api/system-prompts")
async def system_prompts_index(_: Principal = Depends(get_principal)):
    """Lists metadata for the editable sections + global toggles.

    Each section: key, title, source_path (relative to /workspace), present, size,
    toggle_key (config.yaml key that turns the section on/off), generated
    (true if the content is assembled dynamically, with no file)."""
    toggles = _read_system_prompt_config()
    sections: list[dict] = []
    # (key, title, path, toggle_key, generated, read_only)
    fixed = [
        ("platform", "Platform (framework invariants)", PLATFORM_PROMPT_PATH,
         "include_platform_prompt", False, True),
        ("context", "Company context", COMPANY_CONTEXT_PATH,
         "include_company_context", False, False),
        ("philosophy", "Operational philosophy", COMPANY_PHILOSOPHY_PATH,
         "include_company_philosophy", False, False),
    ]
    for key, title, path, toggle_key, generated, read_only in fixed:
        st = path.stat() if path.exists() else None
        sections.append({
            "key": key,
            "title": title,
            "source_path": str(path).replace("/workspace/", ""),
            "present": st is not None,
            "size": st.st_size if st else 0,
            "toggle_key": toggle_key,
            "enabled": toggles.get(toggle_key, True),
            "generated": generated,
            "read_only": read_only,
        })
    # Dynamic contextual blocks (D-110+) — generated on each spawn.
    dynamic_blocks = [
        ("invocation_context",
         "Invocation mode (root vs child)",
         "include_invocation_context"),
        ("task_state",
         "Task state (snapshot when topic = task-*)",
         "include_task_state"),
        ("step_instructions",
         "Current phase instructions (workflows.yaml.steps.<step>.instructions)",
         "include_step_instructions"),
    ]
    for key, title, toggle_key in dynamic_blocks:
        sections.append({
            "key": key,
            "title": title,
            "source_path": None,
            "present": True,
            "size": 0,
            "toggle_key": toggle_key,
            "enabled": toggles.get(toggle_key, True),
            "generated": True,
            "read_only": True,
        })
    # Team block — generated, no file.
    sections.append({
        "key": "team",
        "title": "Team (generated dynamically from DB)",
        "source_path": None,
        "present": True,
        "size": 0,
        "toggle_key": "include_team_block",
        "enabled": toggles.get("include_team_block", True),
        "generated": True,
        "read_only": True,
    })
    # Per agent: 1 entry per active bot agent (is_active=true).
    rows = await db.fetch_all(
        """SELECT u.agent_name AS name, u.full_name AS display_name
             FROM messaging.users u
            WHERE u.kind = 'bot' AND u.agent_name IS NOT NULL
              AND u.is_active = true
            ORDER BY u.agent_name"""
    )
    agents = []
    for r in rows:
        path = _agent_claude_md_path(r["name"])
        st = path.stat() if path.exists() else None
        agents.append({
            "key": f"agent:{r['name']}",
            "name": r["name"],
            "display_name": r["display_name"],
            "title": f"CLAUDE.md — {r['display_name']} ({r['name']})",
            "source_path": f"agents/{r['name']}/CLAUDE.md",
            "present": st is not None,
            "size": st.st_size if st else 0,
            "toggle_key": "include_agent_claude_md",
            "enabled": toggles.get("include_agent_claude_md", True),
            "generated": False,
        })
    return {
        "toggles": toggles,
        "sections": sections,
        "agents": agents,
    }


@app.get("/api/system-prompts/config")
async def system_prompts_config_get(_: Principal = Depends(get_principal)):
    return {"toggles": _read_system_prompt_config()}


@app.put("/api/system-prompts/config")
async def system_prompts_config_set(payload: dict, _: Principal = Depends(get_principal)):
    toggles_in = payload.get("toggles") or {}
    if not isinstance(toggles_in, dict):
        raise HTTPException(status_code=400, detail="toggles must be an object")
    current = _read_system_prompt_config()
    for key, val in toggles_in.items():
        if key not in SYSTEM_PROMPT_TOGGLE_DEFAULTS:
            raise HTTPException(status_code=400, detail=f"unknown toggle: {key!r}")
        if not isinstance(val, bool):
            raise HTTPException(status_code=400, detail=f"{key}: must be boolean")
        current[key] = val
    _write_system_prompt_config(current)
    return {"ok": True, "toggles": current}


@app.get("/api/system-prompts/sections/{key:path}")
async def system_prompts_section_get(key: str, _: Principal = Depends(get_principal)):
    path, content = _read_section(key)
    return {
        "key": key,
        "source_path": str(path).replace("/workspace/", ""),
        "content": content,
        "present": path.exists(),
    }


@app.put("/api/system-prompts/sections/{key:path}")
async def system_prompts_section_set(
    key: str, payload: dict, _: Principal = Depends(get_principal)
):
    size = _write_section(key, payload.get("content", ""))
    return {"ok": True, "size": size}


def _preview_invocation_block(mode: str | None, parent: str | None) -> str:
    """Mirrors claude_runner._invocation_context_block. At runtime the block
    only appears in child convs (root doesn't need it — '## Team' below
    already signals callable peers). In preview with `mode` absent, we show
    a hint."""
    if mode is None:
        return (
            "\n\n## Invocation mode\n\n"
            "_(Preview: pass `?mode=child&parent=<agent>` to simulate the block "
            "the framework injects in a child conv. In root, the block is "
            "omitted; the '## Team' below lists callable peers.)_\n"
        )
    if mode == "root":
        # Root: framework injects no block. Preview shows hint.
        return (
            "\n\n_(Preview: in root mode the framework does not inject "
            "'## Invocation mode'. The '## Team' block below lists callable peers.)_\n"
        )
    if mode == "child":
        parent_label = parent or "unknown agent"
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
    return ""


async def _preview_task_state_block(slug: str | None) -> str:
    if slug is None:
        return (
            "\n\n## Task state\n\n"
            "_(Preview: pass `?task_slug=<slug>` in the URL to simulate this block. "
            "At runtime, the framework injects it when `topic = task-<slug>`.)_\n"
        )
    task = await db.fetch_one(
        """SELECT id, slug, title, workflow, status, current_step,
                  current_agent, complexity, impact, difficulty,
                  origin_stream, origin_topic, blocked_reason, metadata_extra
             FROM tasks.tasks
            WHERE slug = $1 AND archived_at IS NULL""",
        slug,
    )
    if task is None:
        return (
            "\n\n## Task state\n\n"
            f"_(Preview: task `{slug}` not found in the database.)_\n"
        )
    phases = await db.fetch_all(
        """SELECT step, agent, artifact, completed_at FROM tasks.phases
            WHERE task_id = $1 AND completed_at IS NOT NULL ORDER BY completed_at""",
        task["id"],
    )
    worktrees = await db.fetch_all(
        """SELECT repo, path, branch FROM tasks.worktrees
            WHERE task_id = $1 ORDER BY repo""",
        task["id"],
    )
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


def _format_step_overrides_footer(overrides: dict | None) -> str:
    """Visual debug footer: shows which fields the step overrides in
    claude_runner. Empty if overrides is missing/empty."""
    if not isinstance(overrides, dict) or not overrides:
        return ""
    bits: list[str] = []
    if "model" in overrides:
        bits.append(f"model=`{overrides['model']}`")
    if "effort" in overrides:
        bits.append(f"effort=`{overrides['effort']}`")
    mem = overrides.get("memory") or {}
    if isinstance(mem, dict):
        mem_bits: list[str] = []
        if "enabled" in mem:
            mem_bits.append(f"enabled={mem['enabled']}")
        if "auto_inject_limit" in mem:
            mem_bits.append(f"auto_inject_limit={mem['auto_inject_limit']}")
        if mem_bits:
            bits.append("memory={" + ", ".join(mem_bits) + "}")
    if not bits:
        return ""
    return "\n_Active overrides: " + ", ".join(bits) + "._\n"


async def _preview_step_instructions_block(slug: str | None) -> str:
    if slug is None:
        return ""
    row = await db.fetch_one(
        """SELECT workflow, current_step FROM tasks.tasks
            WHERE slug = $1 AND archived_at IS NULL""",
        slug,
    )
    if row is None:
        return ""
    wf_name = row["workflow"]
    step_name = row["current_step"]
    if not wf_name or not step_name:
        return ""
    wf_doc = _load_workflows_full()
    wf = (wf_doc.get("workflows") or {}).get(wf_name) or {}
    step = (wf.get("steps") or {}).get(step_name) or {}
    overrides = step.get("overrides") if isinstance(step, dict) else None
    overrides_footer = _format_step_overrides_footer(overrides)
    instructions = step.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        return (
            "\n\n## Current phase instructions\n\n"
            f"_(Workflow `{wf_name}` -> step `{step_name}` has no "
            "`instructions` field declared in `workflows.yaml`.)_\n"
            + overrides_footer
        )
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
    return "\n".join(header_lines) + instructions.rstrip() + "\n" + overrides_footer


@app.get("/api/system-prompts/preview")
async def system_prompts_preview(
    agent: str | None = None,
    mode: str | None = None,
    parent: str | None = None,
    task_slug: str | None = None,
    _: Principal = Depends(get_principal),
):
    """Assembles the full system prompt as it will be sent to `claude -p` —
    honors the current toggles and simulates the invocation context via params.

    Params (optional, to simulate the contextual blocks):
    - `mode`: `root` or `child`. Without it, the "## Invocation mode" block shows a hint.
    - `parent`: parent agent name (only meaningful with `mode=child`).
    - `task_slug`: slug of an existing task. When given, the
      "## Task state" and "## Current phase instructions" blocks are filled
      from the DB + workflows.yaml.

    Reproduces the logic of claude_runner._build_system_prompt — keep it
    in sync if that changes."""
    toggles = _read_system_prompt_config()
    parts: list[str] = []

    if toggles["include_platform_prompt"]:
        try:
            parts.append(PLATFORM_PROMPT_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            parts.append(
                "_(platform.md missing from image — rebuild web (and agent) so "
                "`COPY framework/system_prompts` ships the file.)_"
            )

    if toggles.get("include_invocation_context", True):
        parts.append(_preview_invocation_block(mode, parent))

    if toggles.get("include_task_state", True):
        block = await _preview_task_state_block(task_slug)
        if block:
            parts.append(block)
    if toggles.get("include_step_instructions", True):
        block = await _preview_step_instructions_block(task_slug)
        if block:
            parts.append(block)

    if toggles["include_agent_claude_md"] and agent:
        path = _agent_claude_md_path(agent)
        try:
            txt = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            txt = ""
        if txt.strip():
            parts.append("\n\n# Agent instructions\n\n" + txt)

    if toggles["include_company_context"]:
        try:
            ctx = COMPANY_CONTEXT_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            ctx = ""
        if ctx.strip():
            parts.append("\n\n# Company context\n\n" + ctx)

    if toggles["include_company_philosophy"]:
        try:
            phi = COMPANY_PHILOSOPHY_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            phi = ""
        if phi.strip() and "_(optional" not in phi:
            parts.append("\n\n# Operational philosophy\n\n" + phi)

    if toggles["include_team_block"] and agent and mode != "child":
        # Reproduces the runner's _team_block — same SQL, same whitelist from
        # agent_policies. In a child, the runner omits it (a child cannot call
        # anyone; listing peers would be misinformation). If the runner's
        # logic changes, sync it here.
        policy = await db.fetch_one(
            "SELECT can_ask FROM messaging.agent_policies WHERE agent = $1",
            agent,
        )
        rows = await db.fetch_all(
            """SELECT u.agent_name AS name, u.full_name AS display_name
                 FROM messaging.users u
                WHERE u.kind = 'bot'
                  AND u.agent_name IS NOT NULL
                  AND u.agent_name <> $1
                  AND u.is_active = true
                ORDER BY u.agent_name""",
            agent,
        )
        whitelist = (policy["can_ask"] if policy and policy["can_ask"] is not None else None)
        peers = []
        for r in rows:
            name = r["name"]
            if whitelist is not None and name not in whitelist:
                continue
            peers.append(f"- **{name}** ({r['display_name']})")
        if peers:
            parts.append(
                "\n\n## Team (agents you can call via `ask_agent` or `ask_agents_many`)\n\n"
                + "\n".join(peers)
            )
        else:
            parts.append(
                "\n\n## Team\n\n"
                "_No other agents available for `ask_agent` right now. "
                "Use `ask_human` if you need to delegate._"
            )

    return {"agent": agent, "toggles": toggles, "content": "".join(parts)}


# ---------- Cost budgets ----------

@app.get("/api/cost-budgets")
async def cost_budgets_list(_: Principal = Depends(get_principal)):
    """Lists configured budgets + today's (UTC) spend per agent."""
    rows = await db.fetch_all(
        """
        WITH today_spend AS (
            SELECT agent, COALESCE(SUM(cost_usd), 0)::float AS spent
              FROM telemetry.events
             WHERE (ts AT TIME ZONE 'UTC')::date = (now() AT TIME ZONE 'UTC')::date
             GROUP BY agent
        )
        SELECT b.agent,
               b.daily_usd_limit::float AS daily_usd_limit,
               b.alert_message,
               COALESCE(t.spent, 0) AS spent_today_usd
          FROM web.cost_budgets b
          LEFT JOIN today_spend t ON t.agent = b.agent
         ORDER BY b.agent
        """,
    )
    return {
        "items": [
            {
                "agent": r["agent"],
                "daily_usd_limit": r["daily_usd_limit"],
                "alert_message": r["alert_message"],
                "spent_today_usd": float(r["spent_today_usd"]),
                "pct": (
                    float(r["spent_today_usd"]) / r["daily_usd_limit"]
                    if r["daily_usd_limit"] else 0
                ),
                "over": float(r["spent_today_usd"]) >= r["daily_usd_limit"],
            } for r in rows
        ],
    }


@app.post("/api/cost-budgets")
async def cost_budget_upsert(payload: dict, _: Principal = Depends(get_principal)):
    agent = (payload.get("agent") or "").strip()
    limit = payload.get("daily_usd_limit")
    if not agent or not limit:
        raise HTTPException(status_code=400, detail="agent + daily_usd_limit are required")
    try:
        limit_f = float(limit)
        if limit_f <= 0:
            raise ValueError("limit must be positive")
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid daily_usd_limit")
    await db.execute(
        """INSERT INTO web.cost_budgets (agent, daily_usd_limit, alert_message)
           VALUES ($1, $2, $3)
           ON CONFLICT (agent) DO UPDATE SET
             daily_usd_limit = EXCLUDED.daily_usd_limit,
             alert_message = EXCLUDED.alert_message,
             updated_at = now()""",
        agent, limit_f, payload.get("alert_message"),
    )
    return {"ok": True, "agent": agent, "daily_usd_limit": limit_f}


@app.delete("/api/cost-budgets/{agent}")
async def cost_budget_delete(agent: str, _: Principal = Depends(get_principal)):
    await db.execute("DELETE FROM web.cost_budgets WHERE agent = $1", agent)
    return {"ok": True}


# ---------- Memory ----------

@app.get("/api/memory/agents")
async def memory_list_agents():
    rows = await db.fetch_all(
        "SELECT agent, COUNT(*) AS count FROM memory.facts GROUP BY agent ORDER BY agent",
    )
    return {"items": [dict(r) for r in rows]}


@app.get("/api/memory/{agent}")
async def memory_list(agent: str, q: str = "", tag: str | None = None, limit: int = 50):
    params: list = [agent]
    where = ["agent = $1"]
    if q:
        where.append(f"to_tsvector('simple', key || ' ' || value) @@ plainto_tsquery('simple', ${len(params)+1})")
        params.append(q)
    if tag:
        where.append(f"${len(params)+1} = ANY(tags)")
        params.append(tag)
    sql = f"""SELECT id, key, value, tags, created_at, updated_at
                FROM memory.facts
               WHERE {' AND '.join(where)}
               ORDER BY updated_at DESC
               LIMIT {min(int(limit), 500)}"""
    rows = await db.fetch_all(sql, *params)
    items = [
        {
            "id": r["id"], "key": r["key"], "value": r["value"], "tags": r["tags"],
            "created_at": int(r["created_at"].timestamp()),
            "updated_at": int(r["updated_at"].timestamp()),
        } for r in rows
    ]
    total = await db.fetch_one("SELECT COUNT(*) AS c FROM memory.facts WHERE agent = $1", agent)
    return {"items": items, "count": total["c"]}


@app.post("/api/memory/{agent}")
async def memory_save(agent: str, payload: dict):
    key = payload.get("key")
    value = payload.get("value")
    tags = payload.get("tags") or []
    if not key or value is None:
        raise HTTPException(status_code=400, detail="key and value are required")
    await db.execute(
        """INSERT INTO memory.facts (agent, key, value, tags)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (agent, key) DO UPDATE SET
             value = EXCLUDED.value, tags = EXCLUDED.tags, updated_at = now()""",
        agent, key, value, list(tags),
    )
    return {"ok": True}


@app.put("/api/memory/{agent}/{key:path}")
async def memory_edit(agent: str, key: str, payload: dict):
    value = payload.get("value")
    tags = payload.get("tags")
    if value is None and tags is None:
        raise HTTPException(status_code=400, detail="provide 'value' and/or 'tags'")
    sets: list[str] = []
    params: list = [agent, key]
    if value is not None:
        params.append(value)
        sets.append(f"value = ${len(params)}")
    if tags is not None:
        params.append(list(tags))
        sets.append(f"tags = ${len(params)}")
    sets.append("updated_at = now()")
    sql = (
        f"UPDATE memory.facts SET {', '.join(sets)} "
        "WHERE agent = $1 AND key = $2 "
        "RETURNING id, key, value, tags, "
        "EXTRACT(EPOCH FROM created_at)::int AS created_at, "
        "EXTRACT(EPOCH FROM updated_at)::int AS updated_at"
    )
    row = await db.fetch_one(sql, *params)
    if row is None:
        raise HTTPException(status_code=404, detail=f"fact '{key}' does not exist in {agent}")
    return {"ok": True, "item": dict(row)}


@app.delete("/api/memory/{agent}/{key:path}")
async def memory_delete(agent: str, key: str):
    status = await db.execute(
        "DELETE FROM memory.facts WHERE agent = $1 AND key = $2", agent, key,
    )
    if isinstance(status, str) and not status.endswith(" 1"):
        raise HTTPException(status_code=404, detail=f"fact '{key}' does not exist in {agent}")
    return {"ok": True}


# ---------- Onboarding wizard ----------

ONBOARDED_FLAG = Path("/workspace/company/.onboarded")


@app.get("/api/onboard/status")
async def onboard_status(_: Principal = Depends(get_principal)):
    """Detects whether the system needs initial onboarding. Fresh =
    the /workspace/.onboarded flag does NOT exist. Also returns the current
    agent count for the UI to display.

    Side effect: async pre-warm of the agent-executor so step 3 of the
    wizard already finds the container running when the user clicks Generate.
    Fire-and-forget — does not block the status response.
    """
    rows = await db.fetch_all(
        "SELECT COUNT(*) AS c FROM messaging.users WHERE kind='bot'"
    )
    agents_count = int(rows[0]["c"]) if rows else 0

    # Pre-warm: fires in the background without blocking the response. Only for
    # fresh setups (no onboarded flag) — if already onboarded, the hire host
    # is probably already up via compose/scheduler. The slug comes from
    # hire.HIRE_AGENT to cover a custom COMPOSE_PROJECT_NAME.
    if not ONBOARDED_FLAG.exists():
        from . import agent_bootstrap as _ab, hire as _hire
        async def _prewarm():
            try:
                await _ab.ensure_agent_running(_hire.HIRE_AGENT, timeout=60.0)
            except Exception as e:
                log.warning("onboard.prewarm_failed", err=str(e)[:200])
        asyncio.create_task(_prewarm())

    return {
        "fresh": not ONBOARDED_FLAG.exists(),
        "agents_count": agents_count,
        "has_context": COMPANY_CONTEXT_PATH.exists(),
    }


@app.post("/api/onboard/propose-agents")
async def onboard_propose_agents(payload: dict, _: Principal = Depends(get_principal)):
    """Calls the LLM (via the executor container) to propose agents aligned
    with the company+philosophy. Body: {company_md, philosophy_md}.
    Returns {agents: [{slug, display_name, role, why, draft_claude_md}]}."""
    company_md = (payload.get("company_md") or "").strip()
    philosophy_md = (payload.get("philosophy_md") or "").strip()
    if not company_md:
        raise HTTPException(status_code=400, detail="company_md is required")

    from . import hire as _hire
    agent_slug = _hire.HIRE_AGENT
    container_name = _hire._hire_container_name()
    prompt = (
        "You are an architect of a virtual company built on Claude Code agents.\n\n"
        "COMPANY (CONTEXT.md):\n"
        f"```markdown\n{company_md}\n```\n\n"
        "ACTIVE OPERATIONAL PHILOSOPHY:\n"
        f"```markdown\n{philosophy_md or '(none)'}\n```\n\n"
        "TASK: propose 3-7 agents this company should have, "
        "aligned with the philosophy.\n\n"
        "For each agent:\n"
        "- slug: lowercase, no spaces, [a-z0-9-], starts with a letter, max 30 chars\n"
        "- display_name: capitalized, in the language of the company description\n"
        "- role: 1 sentence on what it does\n"
        "- why: 1 sentence justifying why this company needs it\n"
        "- draft_claude_md: ready-to-use CLAUDE.md (~200-400 words), "
        "including role, responsibilities, NON-responsibilities, style, "
        "aligned with the philosophy.\n\n"
        "OUTPUT strictly valid JSON (nothing before or after):\n"
        "{\"agents\":[{\"slug\":\"...\",\"display_name\":\"...\",\"role\":\"...\","
        "\"why\":\"...\",\"draft_claude_md\":\"...\"}]}"
    )

    # Mock for E2E (CLAUDE_MOCK in the executor) or if the container is dead
    if os.environ.get("CLAUDE_MOCK"):
        return {
            "agents": [
                {
                    "slug": "po", "display_name": "Product Owner",
                    "role": "Sets priorities and maintains the backlog", "why": "Every company needs direction",
                    "draft_claude_md": "# PO\n\n_(mock)_\n",
                },
                {
                    "slug": "dev", "display_name": "Developer",
                    "role": "Implements features", "why": "Someone has to write the code",
                    "draft_claude_md": "# Dev\n\n_(mock)_\n",
                },
            ],
        }

    # Auto-bootstrap: make sure the executor is running before trying to
    # exec claude in it. Covers a fresh setup (container never created) +
    # restart cycles (stopped but existing).
    from . import agent_bootstrap as _ab
    try:
        await _ab.ensure_agent_running(agent_slug, timeout=45.0)
    except Exception as e:
        log.error("onboard.bootstrap_failed", agent=agent_slug, err=str(e)[:300])
        raise HTTPException(
            status_code=503,
            detail=(
                f"Could not start agent container '{container_name}'. "
                f"Bootstrap error: {str(e)[:300]}"
            ),
        )

    client = docker.from_env()
    container = client.containers.get(container_name)

    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    hire_model = os.environ.get("HIRE_MODEL", "")
    if hire_model:
        cmd += ["--model", hire_model]
    log.info("onboard.proposing", container=container_name, prompt_len=len(prompt))
    def _run():
        return container.exec_run(cmd, stdout=True, stderr=True, demux=False, user="node",
                                  environment={"HOME": "/home/node"})
    rc, out = await asyncio.get_event_loop().run_in_executor(None, _run)
    if rc != 0:
        raise HTTPException(status_code=502, detail=f"claude rc={rc}: {out[:300] if out else ''}")
    try:
        wrapper = json.loads(out)
        result_text = wrapper.get("result", "")
        # Extract the JSON block from result_text (may come wrapped in ```json ... ```)
        import re as _re
        m = _re.search(r"\{[\s\S]*\"agents\"[\s\S]*\}", result_text)
        if not m:
            raise ValueError("LLM did not return JSON with 'agents'")
        proposal = json.loads(m.group(0))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"parse to JSON failed: {e}; raw: {(out or b'')[:300]}")
    return proposal


@app.post("/api/onboard/apply")
async def onboard_apply(payload: dict, _: Principal = Depends(get_principal)):
    """Applies the wizard: saves CONTEXT.md + philosophy.md + creates N agents
    in batch via hire.apply (skips drafting, uses the already-generated drafts).
    Body: {company_md, philosophy_md, agents: [{slug, display_name, role, draft_claude_md}]}.
    Marks /workspace/.onboarded."""
    from . import hire as _hire
    company_md = payload.get("company_md", "")
    philosophy_md = payload.get("philosophy_md", "")
    agents = payload.get("agents") or []
    if not isinstance(agents, list) or not agents:
        raise HTTPException(status_code=400, detail="agents is required (non-empty list)")

    # Save docs first
    if company_md:
        _write_company_file(COMPANY_CONTEXT_PATH, company_md)
    if philosophy_md:
        _write_company_file(COMPANY_PHILOSOPHY_PATH, philosophy_md)

    # Create each agent via hire.apply with a ready draft. apply generates the
    # yaml_entry from the default agent.yaml.example + the given CLAUDE.md.
    # Reconcile is DEFERRED per agent (skip_reconcile=True) and run ONCE
    # at the end — running reconcile N times in quick succession causes
    # transient race conditions in the broker (concurrent creation of
    # users/streams), resulting in "0 created" even with a correct agents.yaml.
    created: list[str] = []
    errors: list[dict] = []
    for a in agents:
        slug = (a.get("slug") or "").strip()
        display = (a.get("display_name") or slug).strip()
        claude_md = a.get("draft_claude_md") or f"# {display}\n\n{a.get('role','')}\n"
        # minimal yaml_entry (memory + ask_human + ask_agent allowed)
        yaml_entry = (
            f"  - name: {slug}\n"
            f"    display_name: \"{display}\"\n"
            f"    description: \"{(a.get('role') or '').replace(chr(34), '')}\"\n"
            f"    streams: [{slug}]\n"
            f"    pool_size: 2\n"
            f"    idle_timeout_sec: 900\n"
            f"    write_access: [company]\n"
            f"    read_access: []\n"
            f"    memory: true\n"
            f"    allowed_tools:\n"
            f"      - group:files\n"
            f"      - group:human\n"
            f"      - group:agents\n"
            f"      - group:memory\n"
        )
        try:
            await _hire.apply_hire({
                "name": slug,
                "yaml_entry": yaml_entry,
                "claude_md": claude_md,
            }, skip_reconcile=True)
            created.append(slug)
        except Exception as e:
            log.exception("onboard.agent_apply_failed", slug=slug)
            errors.append({"slug": slug, "error": str(e)[:300]})

    # Reconcile a single time at the end, after all files were written —
    # the broker sees a coherent batch and creates N users/streams in one
    # pass. A failure here goes into errors but does not undo the files.
    if created:
        try:
            await asyncio.to_thread(_hire.run_reconcile)
        except Exception as e:
            log.exception("onboard.reconcile_failed", created=created)
            errors.append({"slug": "*reconcile*", "error": str(e)[:500]})

    # Mark onboarded even with partial errors (idempotent)
    try:
        ONBOARDED_FLAG.parent.mkdir(parents=True, exist_ok=True)
        ONBOARDED_FLAG.write_text(
            json.dumps({"created": created, "errors": errors, "at": time.time()}),
            encoding="utf-8",
        )
    except Exception:
        log.exception("onboard.flag_write_failed")

    return {"ok": True, "created": created, "errors": errors}


# ---------- Hire ----------

@app.post("/api/hire/draft")
async def hire_draft(payload: dict):
    from . import hire
    return await hire.draft(payload)


@app.post("/api/hire/apply")
async def hire_apply(payload: dict):
    from . import hire
    return await hire.apply(payload)


# ---------- SPA fallback ----------
# AFTER all API routes and mounts: non-reserved routes return index.html
# for the client-side router's deep links (?ask=<id>, future routes
# /conv/<id>, etc). FastAPI processes routes in registration order, so
# this must be the LAST one.

_RESERVED_PREFIXES = ("api/", "static/", "_app/", "health", "sw.js", "manifest.webmanifest", "docs", "openapi", "redoc")


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str):
    if full_path.startswith(_RESERVED_PREFIXES):
        raise HTTPException(status_code=404)
    f = _index_file()
    if not f.exists():
        raise HTTPException(status_code=503, detail="frontend not installed")
    # D-96: same strategy as `/` — index.html must not be cached
    # (it references hashed bundles; if cached, the app sticks to an old
    # version even after a rebuild). `/_app/*` bundles are immutable via middleware.
    return FileResponse(f, media_type="text/html", headers=_NO_CACHE)
