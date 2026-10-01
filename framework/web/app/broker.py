"""HTTP broker — messaging API (messages/events/subscriptions/users/asks/conversations).

Routes (prefixed with /api):
  POST   /messages                   posts a message to a (stream, topic)
  GET    /messages                   lists messages (filters: stream, topic, since_id)
  GET    /events                     SSE stream of new messages
  POST   /streams                    creates a stream (admin)
  GET    /streams                    lists streams
  DELETE /streams/{name}             removes a stream (admin; 409 if it has convs)
  POST   /subscriptions              subscribes a user/bot to a stream (admin)
  GET    /subscriptions              lists the current principal's subs
  POST   /users                      creates a user/bot (admin)
  GET    /users                      lists users
  GET    /users/me                   returns the current principal
  DELETE /users/{username}           removes a user (admin; 409 if it has messages)
  POST   /asks                       registers a pending ask (used by agents)
  GET    /asks                       lists pending asks (of the principal/admin)
  GET    /conversations              lists conversations with metadata + last message

Authentication: Bearer token via the Authorization header. See auth.py.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
from datetime import datetime, timezone
from typing import Any, AsyncIterator

# D-84: "stuck" threshold — a turn open without run_end for longer than this is
# considered stuck. Read from the same env as main.py (its pair; documented there);
# if they diverge in the future, move it to a shared module.
RUNNER_STUCK_SEC = int(os.environ.get("RUNNER_STUCK_SEC", "600"))

import aiohttp
import asyncpg
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import db
from .auth import Principal, get_principal, require_admin


log = structlog.get_logger("broker")
router = APIRouter(prefix="/api")


# ---------- Models ----------

class MessageIn(BaseModel):
    stream: str
    topic: str
    content: str
    client_id: str | None = None
    # Migration 026: 'regular' (default) triggers a turn in the listener; 'echo'
    # is a visual forward of a child-conv reply (D-100), skips dispatch but
    # stays visible to the human via SSE. Validated by a CHECK in the DB.
    kind: str = "regular"
    # D-87: when the msg creates a new conv (first in (stream, topic)), persists
    # parent_conv_id in the column of the same name. Ignored if the conv already exists
    # (invariant: parent doesn't change after creation). Agents pass it in ask_agent; the reactor
    # passes it in complete_phase. Hierarchy becomes an O(1) FK lookup, no heuristics.
    parent_conv_id: int | None = None
    # Sender override. Only takes effect when the caller is a service token
    # (principal without user_id — scheduler/reactor). Ignored for human tokens
    # (prevents impersonation). The username must exist in messaging.users.
    # Default (None) keeps the legacy behavior: a service token falls back to
    # `system-bot`.
    as_username: str | None = None


class MessageOut(BaseModel):
    id: int
    conversation_id: int
    stream: str
    topic: str
    sender_id: int
    sender_username: str
    content: str
    sent_at: str


class StreamIn(BaseModel):
    name: str
    description: str | None = None


class UserIn(BaseModel):
    email: str
    username: str
    full_name: str
    kind: str                       # 'human' | 'bot'
    agent_name: str | None = None
    is_admin: bool = False


class SubscriptionIn(BaseModel):
    user_id: int
    stream: str


class CursorIn(BaseModel):
    stream: str
    last_read_message_id: int


class AskIn(BaseModel):
    stream: str
    topic: str
    question: str
    context: str | None = None
    blocking: bool = True
    # D-111: kind distinguishes ask_human (human push + Mine badge) from
    # ask_agent (target agent answers, silent for the human).
    kind: str = "ask_human"
    target_agent: str | None = None


# ---------- Helpers ----------

_TOMBSTONE_TTL_SEC = int(os.environ.get("TOMBSTONE_TTL_SEC", "300"))


async def _get_or_create_conversation(
    conn: asyncpg.Connection,
    stream: str,
    topic: str,
    parent_conv_id: int | None = None,
) -> tuple[int, int]:
    """Returns (conversation_id, stream_id). Creates it if it doesn't exist.

    Blocks re-creation if `(stream, topic)` has been in the
    `messaging.deleted_topics` tombstone for less than TOMBSTONE_TTL_SEC (default 5min).
    Reason: when the human DELETEs a conv via the PWA, the backend fires
    cancel on the active runners; the dispatcher posts a cancel confirmation
    msg that would come through here and re-create the conv. The tombstone breaks
    that loop — the dispatcher gets 410 Gone, logs and discards.

    D-87: `parent_conv_id` is persisted in the column of the same name when the conv is
    created now (INSERT succeeded). Ignored if the conv already existed (ON CONFLICT
    DO UPDATE path) — invariant: parent doesn't change after creation. Cycles
    and invalid FKs are validated first; fails silently (log warning) instead
    of raising, so as not to break the flow of a msg that was already accepted.
    """
    ts_row = await conn.fetchrow(
        """SELECT dt.deleted_at
             FROM messaging.deleted_topics dt
             JOIN messaging.streams s ON s.id = dt.stream_id
            WHERE s.name = $1 AND dt.topic_name = $2
              AND dt.deleted_at > now() - ($3 || ' seconds')::interval
            LIMIT 1""",
        stream, topic, str(_TOMBSTONE_TTL_SEC),
    )
    if ts_row is not None:
        raise HTTPException(
            status_code=410,
            detail=f"topic {topic!r} was deleted recently; posts are blocked",
        )
    # Defensive validation of parent_conv_id: it must exist, not create a cycle,
    # AND (D-96) the parent must itself be a root. Max tree depth
    # is 1: root -> child. Trying to create a grandchild returns 409 — an agent that
    # became a child via ask_agent must not delegate further; it should reply to the parent,
    # which decides. An explicit error helps catch wrong prompts/code.
    safe_parent: int | None = None
    if parent_conv_id is not None:
        parent_row = await conn.fetchrow(
            "SELECT id, parent_conv_id FROM messaging.conversations WHERE id = $1",
            parent_conv_id,
        )
        if parent_row is None:
            log.warning("broker.invalid_parent_conv_id", parent_conv_id=parent_conv_id, stream=stream, topic=topic)
        elif parent_row["parent_conv_id"] is not None:
            log.warning(
                "broker.depth_violation",
                parent_conv_id=parent_conv_id,
                grandparent_conv_id=parent_row["parent_conv_id"],
                stream=stream, topic=topic,
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    f"depth violation: conv {parent_conv_id} is already a child "
                    f"(parent={parent_row['parent_conv_id']}); cannot have its "
                    "own child. Max hierarchy is root -> child (D-96). "
                    "A child needing more info should reply to its parent, "
                    "not delegate."
                ),
            )
        else:
            safe_parent = parent_conv_id
    row = await conn.fetchrow(
        """
        WITH s AS (SELECT id FROM messaging.streams WHERE name = $1),
        conv_ins AS (
            INSERT INTO messaging.conversations (stream_id, topic_name, parent_conv_id)
            SELECT id, $2, $3 FROM s
            ON CONFLICT (stream_id, topic_name) DO UPDATE SET last_message_at = now()
            RETURNING id, stream_id, (xmax = 0) AS inserted
        )
        SELECT id, stream_id, inserted FROM conv_ins
        """,
        stream, topic, safe_parent,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"stream {stream!r} does not exist")
    return row["id"], row["stream_id"]


# ---------- Messages ----------

@router.post("/messages", response_model=MessageOut)
async def post_message(msg: MessageIn, principal: Principal = Depends(get_principal)) -> MessageOut:
    """Sends a message. Creates the conversation if it doesn't exist.
    Auto-resolves pending_ask if the message comes from someone other than the asker.
    """
    if principal.user_id is None:
        # Service tokens (reactor/scheduler) post as system-bot by default,
        # or as `as_username` if given (the scheduler uses it to post on
        # behalf of a human/bot and make the conv show up for that user).
        if msg.as_username:
            row = await db.fetch_one(
                "SELECT id, username FROM messaging.users WHERE username = $1",
                msg.as_username,
            )
            if row is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"as_username {msg.as_username!r} does not exist",
                )
            sender_id = row["id"]
            effective_username = row["username"]
        else:
            row = await db.fetch_one(
                "SELECT id FROM messaging.users WHERE username = 'system-bot' AND kind = 'bot'",
            )
            if row is None:
                raise HTTPException(status_code=500, detail="system-bot not initialized")
            sender_id = row["id"]
            effective_username = "system-bot"
    else:
        sender_id = principal.user_id
        effective_username = principal.username

    async with db.connection() as conn:
        async with conn.transaction():
            conv_id, stream_id = await _get_or_create_conversation(
                conn, msg.stream, msg.topic,
                parent_conv_id=msg.parent_conv_id,
            )
            # D-93 (generalizes D-76/D-77): a completed child conv becomes read-only
            # for the human. Child = `parent_conv_id IS NOT NULL`. Completed =
            # the agent's last run finished (run_end with subtype ok), no
            # open ask_human, and the last msg is from the stream's bot and is not a
            # question. Classic case: ask_agent reply (D-76), task-<slug>
            # child after complete_phase, notify_human terminal.
            #
            # Who can still post: the `<stream>-bot` itself (legitimate
            # post-reply output, rare but it happens) and any service token
            # (reactor posting a handoff back into the conv). The block targets
            # a human who might reply in the wrong place.
            conv_meta = await conn.fetchrow(
                """SELECT c.parent_conv_id,
                          (SELECT u.username FROM messaging.messages m
                             JOIN messaging.users u ON u.id = m.sender_id
                            WHERE m.conversation_id = c.id
                            ORDER BY m.id DESC LIMIT 1) AS last_sender,
                          (SELECT m.content FROM messaging.messages m
                            WHERE m.conversation_id = c.id
                            ORDER BY m.id DESC LIMIT 1) AS last_content,
                          (SELECT le.kind FROM telemetry.live_events le
                            WHERE le.conversation_id = c.id
                              AND le.kind IN ('run_start', 'run_end')
                            ORDER BY le.id DESC LIMIT 1) AS last_run_kind,
                          (SELECT le.data->>'subtype' FROM telemetry.live_events le
                            WHERE le.conversation_id = c.id
                              AND le.kind IN ('run_start', 'run_end')
                            ORDER BY le.id DESC LIMIT 1) AS last_run_subtype,
                          EXISTS(SELECT 1 FROM messaging.pending_asks pa
                                  WHERE pa.conversation_id = c.id
                                    AND pa.resolved_at IS NULL
                                    AND pa.kind = 'ask_human') AS has_open_ask
                     FROM messaging.conversations c
                    WHERE c.id = $1""",
                conv_id,
            )
            # D-96: any child conv is read-only for the human. Before (D-93)
            # only a completed child was; now it's generalized — the human only replies
            # at the root, and the root agent decides whether to delegate/escalate. Bots (service
            # tokens, agents via broker token) can still post —
            # the block targets a human who would pick the wrong conv.
            if (
                conv_meta
                and conv_meta["parent_conv_id"] is not None
                and principal.kind == "human"
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "This is a delegated sub-conversation (read-only). "
                        "Reply on the parent conversation; the parent agent "
                        "will pass it through."
                    ),
                )
            try:
                m_row = await conn.fetchrow(
                    """
                    INSERT INTO messaging.messages (conversation_id, sender_id, content, client_id, kind)
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING id, sent_at
                    """,
                    conv_id, sender_id, msg.content, msg.client_id, msg.kind,
                )
            except asyncpg.UniqueViolationError:
                # idempotency: client_id already exists -> return the existing msg
                m_row = await conn.fetchrow(
                    """
                    SELECT id, sent_at FROM messaging.messages
                    WHERE conversation_id = $1 AND client_id = $2
                    """,
                    conv_id, msg.client_id,
                )
            # auto-resolve pending_ask (if it exists and sender != asker).
            # Migration 026: kind='echo' is a visual forward, not an answer —
            # skip auto-resolve so we don't close an open ask_human in the parent conv
            # based on an echo that came from another conv.
            if msg.kind == "regular":
                await conn.execute(
                    """
                    UPDATE messaging.pending_asks
                       SET resolved_at = now(), answer_message_id = $2
                     WHERE conversation_id = $1
                       AND asker_id <> $3
                       AND resolved_at IS NULL
                    """,
                    conv_id, m_row["id"], sender_id,
                )
    log.info("broker.message_posted", conv_id=conv_id, sender=effective_username, stream=msg.stream, topic=msg.topic)
    return MessageOut(
        id=m_row["id"],
        conversation_id=conv_id,
        stream=msg.stream,
        topic=msg.topic,
        sender_id=sender_id,
        sender_username=effective_username,
        content=msg.content,
        sent_at=m_row["sent_at"].isoformat(),
    )


@router.get("/messages")
async def list_messages(
    stream: str | None = None,
    topic: str | None = None,
    conversation_id: int | None = None,
    since_id: int = 0,
    limit: int = Query(default=100, le=500),
    principal: Principal = Depends(get_principal),
):
    """Lists messages, most recent last. Filters are cumulative."""
    where = ["m.id > $1"]
    params: list[Any] = [since_id]
    if conversation_id is not None:
        where.append(f"m.conversation_id = ${len(params)+1}")
        params.append(conversation_id)
    if stream is not None:
        where.append(f"s.name = ${len(params)+1}")
        params.append(stream)
    if topic is not None:
        where.append(f"c.topic_name = ${len(params)+1}")
        params.append(topic)
    sql = f"""
        SELECT m.id, m.conversation_id, s.name AS stream, c.topic_name AS topic,
               m.sender_id, u.username AS sender_username, m.content, m.sent_at,
               m.kind
          FROM messaging.messages m
          JOIN messaging.conversations c ON c.id = m.conversation_id
          JOIN messaging.streams s ON s.id = c.stream_id
          JOIN messaging.users u ON u.id = m.sender_id
         WHERE {' AND '.join(where)}
         ORDER BY m.id ASC
         LIMIT {int(limit)}
    """
    rows = await db.fetch_all(sql, *params)
    return [
        dict(
            id=r["id"], conversation_id=r["conversation_id"], stream=r["stream"],
            topic=r["topic"], sender_id=r["sender_id"], sender_username=r["sender_username"],
            content=r["content"], sent_at=r["sent_at"].isoformat(),
            kind=r["kind"],
        ) for r in rows
    ]


# ---------- Events SSE ----------

@router.get("/events")
async def events_sse(
    request: Request,
    streams: str | None = Query(default=None, description="comma-separated stream names; omit = all"),
    principal: Principal = Depends(get_principal),
):
    """SSE stream: emits each new message as `data: {...}\\n\\n`.
    Clients must reconnect if it drops (browser EventSource does it automatically).
    """
    filter_names = [s.strip() for s in streams.split(",")] if streams else None

    async def event_gen() -> AsyncIterator[str]:
        import os
        # Dedicated connection outside the pool for LISTEN (LISTEN keeps the conn idle).
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        # Tuple (channel, payload) to tell the source apart.
        queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

        def _cb(_conn, _pid, channel, payload):
            queue.put_nowait((channel, payload))
        try:
            # msg_all: main event (new msg) — enriched via SELECT.
            # ask_new: pending_ask INSERT — fixes race D-77 (the msg commits
            # before the pending_ask; without this listen the frontend re-fetches
            # /api/conversations before has_pending_ask turns true).
            # conv_activity: run_start/run_end in telemetry — fixes stale
            # is_queued D-81 (pool frees up, runner spawns, but the sidebar
            # kept showing QUEUED until the next msg).
            await conn.add_listener("msg_all", _cb)
            await conn.add_listener("ask_new", _cb)
            await conn.add_listener("conv_activity", _cb)
            # Heartbeat to keep the connection alive
            while True:
                if await request.is_disconnected():
                    break
                try:
                    channel, payload = await asyncio.wait_for(queue.get(), timeout=15)
                    data = json.loads(payload)
                    if channel == "ask_new":
                        # Slim; the frontend only needs the trigger to re-fetch.
                        # Doesn't go through filter_names (asks are global).
                        yield f"data: {json.dumps(data)}\n\n"
                        continue
                    if channel == "conv_activity":
                        # The frontend only needs to know something changed in the conv
                        # (run_start/run_end) to re-fetch /api/conversations.
                        # payload already has conversation_id + kind + ts.
                        data["_channel"] = "conv_activity"
                        yield f"data: {json.dumps(data)}\n\n"
                        continue
                    # channel == "msg_all": the pg_notify payload is slim
                    # (id + conversation_id + sender_id) — the 8000-byte
                    # limit prevents inlining content. Fetch the rest by id.
                    row = await db.fetch_one(
                        """SELECT s.name AS stream, c.topic_name AS topic,
                                  u.username AS sender_username,
                                  m.content, m.sent_at
                             FROM messaging.messages m
                             JOIN messaging.conversations c ON c.id = m.conversation_id
                             JOIN messaging.streams s ON s.id = c.stream_id
                             JOIN messaging.users u ON u.id = m.sender_id
                            WHERE m.id = $1""",
                        data["id"],
                    )
                    if row is None:
                        continue
                    if filter_names is not None and row["stream"] not in filter_names:
                        continue
                    data["stream"] = row["stream"]
                    data["topic"] = row["topic"]
                    data["sender_username"] = row["sender_username"]
                    data["content"] = row["content"]
                    data["sent_at"] = row["sent_at"].isoformat()
                    yield f"data: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"   # keepalive
        finally:
            await conn.remove_listener("msg_all", _cb)
            await conn.remove_listener("ask_new", _cb)
            await conn.remove_listener("conv_activity", _cb)
            await conn.close()

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# ---------- Streams ----------

@router.post("/streams")
async def create_stream(data: StreamIn, _: Principal = Depends(require_admin)):
    # is_active = true in the UPSERT — if the stream was soft-deleted before
    # (agent left agents.yaml and came back), re-creating it reactivates it.
    row = await db.fetch_one(
        """INSERT INTO messaging.streams (name, description, is_active)
           VALUES ($1, $2, true)
           ON CONFLICT (name) DO UPDATE SET
              description = COALESCE(EXCLUDED.description, messaging.streams.description),
              is_active = true
           RETURNING id, name, description, is_active""",
        data.name, data.description,
    )
    return {"id": row["id"], "name": row["name"], "description": row["description"],
            "is_active": row["is_active"]}


@router.get("/streams")
async def list_streams(principal: Principal = Depends(get_principal)):
    from . import app_settings as _s
    # Includes is_active so the UI can hide inactive ones. By default lists
    # active+inactive (keeps access to history); the UI filters.
    rows = await db.fetch_all(
        "SELECT id, name, description, is_active FROM messaging.streams ORDER BY name"
    )
    streams = [dict(id=r["id"], name=r["name"], description=r["description"],
                    is_active=r["is_active"]) for r in rows]
    default = await _s.get_default_stream()
    return {"streams": streams, "default": default}


@router.patch("/streams/{name}/active")
async def set_stream_active(
    name: str, payload: dict, _: Principal = Depends(require_admin),
):
    """Soft-delete/reactivation. Body: {"is_active": bool}.

    Soft-delete keeps history but removes the stream from UI listings
    and from the system prompt's team section. Used by reconcile when an agent
    leaves agents.yaml — a safe alternative to the hard delete, which fails with
    409 if there are convs.
    """
    if "is_active" not in payload or not isinstance(payload["is_active"], bool):
        raise HTTPException(status_code=400, detail="body requires {'is_active': bool}")
    row = await db.fetch_one(
        "UPDATE messaging.streams SET is_active = $2 WHERE name = $1 "
        "RETURNING id, name, is_active",
        name, payload["is_active"],
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"stream {name!r} does not exist")
    return {"id": row["id"], "name": row["name"], "is_active": row["is_active"]}


@router.delete("/streams/{name}")
async def delete_stream(name: str, _: Principal = Depends(require_admin)):
    """Removes a stream from the broker. Fails with 409 if there are conversations (keeps
    history). Subscriptions without conversations go via CASCADE.

    Used by reconcile to prune streams that left agents.yaml.
    """
    row = await db.fetch_one("SELECT id FROM messaging.streams WHERE name = $1", name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"stream {name!r} does not exist")
    stream_id = row["id"]
    conv_count = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM messaging.conversations WHERE stream_id = $1", stream_id,
    )
    if conv_count["n"] > 0:
        raise HTTPException(
            status_code=409,
            detail=f"stream {name!r} has {conv_count['n']} conversation(s); delete manually via SQL if intentional",
        )
    await db.execute("DELETE FROM messaging.streams WHERE id = $1", stream_id)
    return {"ok": True, "deleted": name}


# ---------- Subscriptions ----------

@router.post("/subscriptions")
async def create_subscription(data: SubscriptionIn, _: Principal = Depends(require_admin)):
    row = await db.fetch_one(
        """
        INSERT INTO messaging.subscriptions (user_id, stream_id)
        SELECT $1, s.id FROM messaging.streams s WHERE s.name = $2
        ON CONFLICT DO NOTHING
        RETURNING user_id, stream_id
        """,
        data.user_id, data.stream,
    )
    if row is None:
        # may have failed because the stream doesn't exist or is already subscribed
        exists = await db.fetch_one("SELECT 1 FROM messaging.streams WHERE name = $1", data.stream)
        if exists is None:
            raise HTTPException(status_code=404, detail=f"stream {data.stream!r} does not exist")
    return {"ok": True}


@router.get("/subscriptions")
async def list_subscriptions(principal: Principal = Depends(get_principal)):
    if principal.user_id is None:
        return []
    rows = await db.fetch_all(
        """SELECT s.id, s.name, s.description
             FROM messaging.subscriptions sub
             JOIN messaging.streams s ON s.id = sub.stream_id
            WHERE sub.user_id = $1
            ORDER BY s.name""",
        principal.user_id,
    )
    return [dict(id=r["id"], name=r["name"], description=r["description"]) for r in rows]


# ---------- Persisted cursor (unread catch-up — D-52) ----------
# pg_notify is fire-and-forget; without a cursor, msgs that arrive while the bot
# is down become ghosts. InternalClient.start() fetches the cursor before
# LISTEN; after processing a msg, it tells the broker to advance the cursor.

@router.get("/subscriptions/cursor")
async def get_subscription_cursor(stream: str, principal: Principal = Depends(get_principal)):
    if principal.user_id is None:
        raise HTTPException(status_code=401, detail="missing user_id")
    row = await db.fetch_one(
        """SELECT sub.last_read_message_id
             FROM messaging.subscriptions sub
             JOIN messaging.streams s ON s.id = sub.stream_id
            WHERE sub.user_id = $1 AND s.name = $2""",
        principal.user_id, stream,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"not subscribed to {stream!r}")
    return {"stream": stream, "last_read_message_id": row["last_read_message_id"]}


@router.post("/subscriptions/cursor")
async def set_subscription_cursor(data: CursorIn, principal: Principal = Depends(get_principal)):
    """Advances the cursor monotonically — UPDATE only if the new id is greater than
    the current one (avoids regression from a race between catch-up and a near-simultaneous LISTEN).
    """
    if principal.user_id is None:
        raise HTTPException(status_code=401, detail="missing user_id")
    row = await db.fetch_one(
        """UPDATE messaging.subscriptions sub
              SET last_read_message_id = GREATEST(
                    COALESCE(sub.last_read_message_id, 0),
                    $3
                  )
             FROM messaging.streams s
            WHERE s.id = sub.stream_id
              AND sub.user_id = $1 AND s.name = $2
         RETURNING sub.last_read_message_id""",
        principal.user_id, data.stream, data.last_read_message_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"not subscribed to {data.stream!r}")
    return {"ok": True, "last_read_message_id": row["last_read_message_id"]}


# ---------- Users ----------

@router.post("/users")
async def create_user(data: UserIn, _: Principal = Depends(require_admin)):
    api_token = None
    if data.kind == "bot":
        api_token = secrets.token_hex(32)
    # is_active = true in the UPSERT — reactivates a soft-deleted user (agent that
    # left and came back to agents.yaml).
    row = await db.fetch_one(
        """INSERT INTO messaging.users (email, username, full_name, kind, agent_name, api_token, is_admin, is_active)
           VALUES ($1, $2, $3, $4, $5, $6, $7, true)
           ON CONFLICT (email) DO UPDATE SET
              username = EXCLUDED.username, full_name = EXCLUDED.full_name,
              kind = EXCLUDED.kind, agent_name = EXCLUDED.agent_name,
              is_admin = EXCLUDED.is_admin,
              is_active = true,
              -- keep the existing api_token unless it was rotated
              api_token = COALESCE(messaging.users.api_token, EXCLUDED.api_token)
           RETURNING id, email, username, full_name, kind, agent_name, api_token, is_admin, is_active""",
        data.email, data.username, data.full_name, data.kind, data.agent_name, api_token, data.is_admin,
    )
    return dict(
        id=row["id"], email=row["email"], username=row["username"],
        full_name=row["full_name"], kind=row["kind"], agent_name=row["agent_name"],
        is_admin=row["is_admin"], api_token=row["api_token"], is_active=row["is_active"],
    )


@router.get("/users")
async def list_users(_: Principal = Depends(require_admin)):
    rows = await db.fetch_all(
        """SELECT id, email, username, full_name, kind, agent_name, is_admin, is_active
             FROM messaging.users ORDER BY id""",
    )
    return [dict(r) for r in rows]


@router.patch("/users/{username}/active")
async def set_user_active(
    username: str, payload: dict, _: Principal = Depends(require_admin),
):
    """User soft-delete/reactivation. Body: {"is_active": bool}.

    Used by reconcile to deactivate bots of agents that left
    agents.yaml. Keeps history (messages.sender_id), but removes the
    user from UI listings and from the peers' system prompt team section.
    """
    if "is_active" not in payload or not isinstance(payload["is_active"], bool):
        raise HTTPException(status_code=400, detail="body requires {'is_active': bool}")
    row = await db.fetch_one(
        "UPDATE messaging.users SET is_active = $2 WHERE username = $1 "
        "RETURNING id, username, is_active",
        username, payload["is_active"],
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"user {username!r} does not exist")
    return {"id": row["id"], "username": row["username"], "is_active": row["is_active"]}


@router.get("/users/me")
async def whoami(principal: Principal = Depends(get_principal)):
    return dict(user_id=principal.user_id, username=principal.username, kind=principal.kind, is_admin=principal.is_admin)


@router.delete("/users/{username}")
async def delete_user(username: str, _: Principal = Depends(require_admin)):
    """Removes a user from the broker. Fails with 409 if the user has sent messages (keeps
    history — messages.sender_id has an FK without CASCADE). Subscriptions go via
    CASCADE.

    Used by reconcile to prune bots that left agents.yaml.
    """
    row = await db.fetch_one("SELECT id, kind FROM messaging.users WHERE username = $1", username)
    if row is None:
        raise HTTPException(status_code=404, detail=f"user {username!r} does not exist")
    msg_count = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM messaging.messages WHERE sender_id = $1", row["id"],
    )
    if msg_count["n"] > 0:
        raise HTTPException(
            status_code=409,
            detail=f"user {username!r} has {msg_count['n']} message(s) in history; cannot be deleted",
        )
    await db.execute("DELETE FROM messaging.users WHERE id = $1", row["id"])
    return {"ok": True, "deleted": username}


# ---------- Pending asks ----------

@router.post("/asks")
async def create_ask(data: AskIn, principal: Principal = Depends(get_principal)):
    """Registers a pending_ask. Called by the agent inside ask_human/ask_agent.
    The conversation should exist (post_message should have been called first
    with the question itself, for history); if not, it is created.

    D-111: `kind` distinguishes 'ask_human' (default — the human must answer, push
    + Mine badge) from 'ask_agent' (target_agent must answer, silent).
    If a resolved pending_ask already exists for the same conv (idempotency on
    restart-recovery), returns the current state with `resolved=true` and
    `answer_message_id` — the caller uses it to detect the ask was already answered.
    """
    if principal.user_id is None:
        raise HTTPException(status_code=400, detail="pending_ask requires valid user_id (service tokens cannot ask)")
    if data.kind not in ("ask_human", "ask_agent"):
        raise HTTPException(status_code=400, detail=f"invalid kind: {data.kind!r}")
    async with db.connection() as conn:
        async with conn.transaction():
            conv_id, _ = await _get_or_create_conversation(conn, data.stream, data.topic)
            # Idempotency check: if a pending_ask already exists for this conv
            # and was already resolved (target answered), return the current state
            # without overwriting — the caller (ask_agent_via_callback after a restart)
            # reads `resolved=true` and fetches the answer directly.
            existing = await conn.fetchrow(
                """SELECT conversation_id, asked_at, resolved_at, answer_message_id, kind
                     FROM messaging.pending_asks
                    WHERE conversation_id = $1""",
                conv_id,
            )
            if existing is not None and existing["resolved_at"] is not None:
                return {
                    "conversation_id": existing["conversation_id"],
                    "asked_at": existing["asked_at"].isoformat(),
                    "resolved": True,
                    "resolved_at": existing["resolved_at"].isoformat(),
                    "answer_message_id": existing["answer_message_id"],
                    "kind": existing["kind"],
                }
            row = await conn.fetchrow(
                """INSERT INTO messaging.pending_asks
                       (conversation_id, asker_id, question, context, blocking, kind, target_agent)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)
                   ON CONFLICT (conversation_id) DO UPDATE SET
                     question = EXCLUDED.question, context = EXCLUDED.context,
                     blocking = EXCLUDED.blocking, asked_at = now(),
                     kind = EXCLUDED.kind, target_agent = EXCLUDED.target_agent,
                     resolved_at = NULL, answer_message_id = NULL
                   RETURNING conversation_id, asked_at""",
                conv_id, principal.user_id, data.question, data.context, data.blocking,
                data.kind, data.target_agent,
            )
    return {
        "conversation_id": row["conversation_id"],
        "asked_at": row["asked_at"].isoformat(),
        "resolved": False,
    }


@router.get("/asks")
async def list_asks(principal: Principal = Depends(get_principal)):
    """Lists pending_asks directed at the human (kind='ask_human').
    ask_agent (cross-agent) is deliberately excluded — it's traffic that is
    silent for the human (the target agent answers, not the human).
    """
    if principal.is_admin:
        rows = await db.fetch_all(
            """SELECT pa.conversation_id, s.name AS stream, c.topic_name AS topic,
                      pa.asker_id, ua.username AS asker_username,
                      pa.question, pa.context, pa.blocking, pa.asked_at
                 FROM messaging.pending_asks pa
                 JOIN messaging.conversations c ON c.id = pa.conversation_id
                 JOIN messaging.streams s ON s.id = c.stream_id
                 JOIN messaging.users ua ON ua.id = pa.asker_id
                WHERE pa.resolved_at IS NULL
                  AND pa.kind = 'ask_human'
                ORDER BY pa.asked_at DESC""",
        )
    else:
        rows = []
    return [
        dict(
            conversation_id=r["conversation_id"], stream=r["stream"], topic=r["topic"],
            asker_id=r["asker_id"], asker_username=r["asker_username"],
            question=r["question"], context=r["context"], blocking=r["blocking"],
            asked_at=r["asked_at"].isoformat(),
        ) for r in rows
    ]


# ---------- Conversations (internal helpers — public aliases in main.py) ----------
#
# Single list of threads, filtered by `archived_at` on the conversation (added in
# migration 008). Active = IS NULL; Closed = IS NOT NULL. Manual filter,
# always controlled by the human. Task metadata (workflow, current_step,
# current_agent, status) comes along via LEFT JOIN on `tasks.tasks` by origin_stream/
# origin_topic — when the conversation is a task's origin, the frontend renders a
# progress bar in the header; otherwise it's free chat.

async def list_unified_conversations(
    principal: Principal,
    filter_: str = "active",
) -> list[dict]:
    """Lists conversations the human is involved in, with optional task metadata.
    filter_: 'active' (archived_at IS NULL) or 'closed' (IS NOT NULL).

    Includes:
      - conversations where the human has posted (participating=true), OR
      - conversations with an unresolved pending_ask (some agent asked for the
        human's attention — shows up even if the human hasn't posted yet), OR
      - `__ask-from-*` conversations (ask_agent sub-conversations) — pulled in so
        they can be nested in the sidebar under the parent conv (see _compute_hierarchy).

    Each item comes with `parent_conv_id` (None if root) + `children_stats`
    aggregated recursively (active, awaiting_human, resolved,
    deepest_pending_path). `__ask-from-*` convs with no inferable parent
    stay as root (legacy behavior: they only show up if they have a pending_ask).
    """
    if filter_ not in ("active", "closed"):
        raise HTTPException(status_code=400, detail="filter must be 'active' or 'closed'")
    archived_clause = "c.archived_at IS NULL" if filter_ == "active" else "c.archived_at IS NOT NULL"
    rows = await db.fetch_all(
        f"""
        SELECT c.id, c.created_at, s.name AS stream, c.topic_name AS topic,
               c.custom_title,
               c.last_message_at, c.archived_at, c.parent_conv_id AS persisted_parent_conv_id,
               (SELECT COUNT(*) FROM messaging.messages m WHERE m.conversation_id = c.id) AS msg_count,
               (SELECT m.content FROM messaging.messages m
                  WHERE m.conversation_id = c.id ORDER BY m.id DESC LIMIT 1) AS last_content,
               (SELECT u.username FROM messaging.messages m
                  JOIN messaging.users u ON u.id = m.sender_id
                  WHERE m.conversation_id = c.id ORDER BY m.id DESC LIMIT 1) AS last_sender,
               EXISTS(SELECT 1 FROM messaging.pending_asks pa
                       WHERE pa.conversation_id = c.id AND pa.resolved_at IS NULL
                         AND pa.kind = 'ask_human') AS awaiting_human,
               EXISTS(SELECT 1 FROM messaging.messages m2
                       WHERE m2.conversation_id = c.id AND m2.sender_id = $1) AS participating,
               -- D-84: unified state model. Migration 030 introduced
               -- messaging.runs as the single source of truth — 1 row per
               -- CLI execution, updated transactionally by the broker
               -- when it ingests live_events. Replaced 3 subqueries on
               -- telemetry.live_events with one LATERAL with 1 lookup.
               r.status            AS run_status,
               r.last_heartbeat_at AS run_last_heartbeat_at,
               r.exit_reason       AS run_exit_reason,
               -- Task metadata: first tries to match by origin (the conv that
               -- created the task via the first complete_phase); otherwise matches
               -- by topic = 'task-<slug>' (intermediate convs in the streams
               -- of agents that processed some phase). COALESCE to get
               -- a single task, origin takes priority when both match.
               COALESCE(t.slug,          t_topic.slug)          AS task_slug,
               COALESCE(t.title,         t_topic.title)         AS task_title,
               COALESCE(t.workflow,      t_topic.workflow)      AS task_workflow,
               COALESCE(t.status,        t_topic.status)        AS task_status,
               COALESCE(t.current_step,  t_topic.current_step)  AS task_current_step,
               COALESCE(t.current_agent, t_topic.current_agent) AS task_current_agent,
               COALESCE(t.complexity,    t_topic.complexity)    AS task_complexity,
               -- true when this conv is the task's ORIGIN (match via
               -- origin_stream/origin_topic). Used in _compute_hierarchy
               -- to pick the parent of sibling task-<slug> convs
               -- deterministically — without it, the DESC order by last_message_at
               -- can make a child conv override the origin.
               (t.slug IS NOT NULL)                              AS is_task_origin,
               -- When the task's task_current_agent != this.stream, this conv
               -- (intermediate task-<slug> child in a stream other than the
               -- phase's current agent) has finished its part — the frontend
               -- uses this to treat the child as resolved and not
               -- count it in children_stats.active (avoids a ghost "RUNNING").
               CASE
                 WHEN COALESCE(t.current_agent, t_topic.current_agent) IS NOT NULL
                  AND COALESCE(t.current_agent, t_topic.current_agent) != s.name
                 THEN true
                 ELSE false
               END AS task_not_current_agent
          FROM messaging.conversations c
          JOIN messaging.streams s ON s.id = c.stream_id
          LEFT JOIN LATERAL (
                SELECT status, last_heartbeat_at, exit_reason
                  FROM messaging.runs
                 WHERE conversation_id = c.id
                 ORDER BY started_at DESC LIMIT 1
          ) r ON true
          LEFT JOIN tasks.tasks t
                 ON t.origin_stream = s.name
                AND t.origin_topic = c.topic_name
                AND t.archived_at IS NULL
          LEFT JOIN tasks.tasks t_topic
                 ON c.topic_name = 'task-' || t_topic.slug
                AND t_topic.archived_at IS NULL
         WHERE {archived_clause}
           AND (
                 EXISTS (
                   SELECT 1 FROM messaging.messages m3
                    WHERE m3.conversation_id = c.id AND m3.sender_id = $1
                 )
              OR EXISTS (
                   -- D-111: ask_agent (kind='ask_agent') doesn't pull the conv into
                   -- the human's list — it's agent-to-agent, silent.
                   SELECT 1 FROM messaging.pending_asks pa2
                    WHERE pa2.conversation_id = c.id AND pa2.resolved_at IS NULL
                      AND pa2.kind = 'ask_human'
                 )
              OR c.topic_name LIKE '\\_\\_ask-from-%' ESCAPE '\\'
              OR c.topic_name LIKE '\\_\\_child-%' ESCAPE '\\'
              -- Handoff convs between agents (topic = 'task-<slug>'
              -- in secondary streams) must be included here to serve
              -- as the "middle link" in the tree — otherwise the target's ask_agent
              -- children point to a parent_conv_id that isn't in
              -- byId and become roots in the sidebar. See D-77 followup.
              OR EXISTS (
                   SELECT 1 FROM tasks.tasks t2
                    WHERE c.topic_name = 'task-' || t2.slug
                      AND t2.archived_at IS NULL
                 )
               )
         ORDER BY c.last_message_at DESC
         LIMIT 200
        """,
        principal.user_id or 0,
    )
    out = []
    for r in rows:
        # D-96 cleanup: `ask_replied` (D-76) and `is_completed_child` (D-93)
        # were removed. Every child (parent_conv_id != null) is read-only
        # for the human. The frontend derives read-only directly from parent_conv_id.
        # _compute_hierarchy uses is_running/is_stuck/awaiting_human to
        # pick the active/resolved bucket.

        # D-84: unified state model — 4 flat signals, mutually
        # exclusive by construction via precedence:
        #   awaiting_human > is_stuck > is_running > is_errored > idle
        # Source (migration 030): messaging.runs.status. The reaper moves
        # running -> stale when the heartbeat goes stale, so stuck
        # kicks in automatically when elapsed > RUNNER_STUCK_SEC even before
        # the reaper runs (covers the window between an old heartbeat and the next
        # scheduler pass).
        awaiting_human = bool(r["awaiting_human"])
        run_status = r["run_status"]
        heartbeat_at = r["run_last_heartbeat_at"]
        elapsed: float | None = None
        if heartbeat_at is not None:
            elapsed = (datetime.now(tz=timezone.utc) - heartbeat_at).total_seconds()
        is_stuck = bool(
            run_status == "running"
            and elapsed is not None
            and elapsed > RUNNER_STUCK_SEC
            and not awaiting_human
        )
        is_running = bool(
            run_status == "running"
            and not is_stuck
            and not awaiting_human
        )
        is_errored = bool(
            run_status == "error"
            and not awaiting_human
        )
        item = dict(
            id=r["id"], stream=r["stream"], topic=r["topic"],
            custom_title=r["custom_title"],
            created_at=r["created_at"].isoformat() if r["created_at"] else None,
            last_message_at=r["last_message_at"].isoformat(),
            archived_at=r["archived_at"].isoformat() if r["archived_at"] else None,
            msg_count=r["msg_count"], last_content=r["last_content"], last_sender=r["last_sender"],
            awaiting_human=awaiting_human,
            participating=r["participating"],
            is_running=is_running,
            is_stuck=is_stuck,
            is_errored=is_errored,
        )
        if r["task_slug"]:
            item["task"] = dict(
                slug=r["task_slug"],
                title=r["task_title"],
                workflow=r["task_workflow"],
                status=r["task_status"],
                current_step=r["task_current_step"],
                current_agent=r["task_current_agent"],
                complexity=r["task_complexity"],
            )
        # D-79: "intermediate child already finished" signal — used by the
        # store's isResolvedSelf so `task-<slug>` convs from
        # old phases don't count as the parent's children_active (the current agent is
        # in another stream, this child won't run again for this task).
        item["task_not_current_agent"] = bool(r["task_not_current_agent"])
        # D-87: parent_conv_id persisted in the schema — pre-fills the item.
        # `_compute_hierarchy` uses it directly if present; the old heuristic
        # is only a fallback for legacy (pre-migration) convs with NULL.
        item["_persisted_parent_conv_id"] = r["persisted_parent_conv_id"]
        # Used in _compute_hierarchy to anchor the right parent when several
        # convs share topic = 'task-<slug>'. Only one of them is the real
        # origin (JOIN via origin_stream/origin_topic in the SQL above).
        item["_is_task_origin"] = bool(r["is_task_origin"])
        out.append(item)

    # Computes parent-child + aggregated stats. No new schema: derived from
    # topic patterns + telemetry.live_events + tasks.tasks. See helper.
    await _compute_hierarchy(out)
    # Internal fields used only by _compute_hierarchy — they don't leak into the JSON.
    for it in out:
        it.pop("_is_task_origin", None)
        it.pop("_persisted_parent_conv_id", None)
    return out


async def _compute_hierarchy(items: list[dict]) -> None:
    """Annotates each item with `parent_conv_id` and `children_stats`.

    D-96 cleanup: the old heuristic (Phase 1 ask_agent tool_use parsing,
    Phase 2 task.slug siblings inference, cycle detection) was removed —
    the single source is `messaging.conversations.parent_conv_id`, persisted
    via SQL (D-87 + reactor handoffs). Legacy pre-D-87 convs without the
    field show up as roots — acceptable.

    Max depth is 1 by construction (D-96: the broker rejects with 409
    on a depth violation), so the aggregate is trivial: 1 level of children
    per root, no recursion.

    children_stats:
      - active: active children (running/stuck/awaiting_human, or
        a non-terminal task without completed_child)
      - stuck: children with is_stuck
      - resolved: children that have finished their turn
    """
    if not items:
        return
    by_id: dict[int, dict] = {it["id"]: it for it in items}

    # Annotate parent_conv_id straight from the schema. Convs whose parent isn't among
    # the loaded items are orphaned (parent_conv_id None in the response) —
    # the listing doesn't show hierarchy across already-archived roots.
    for it in items:
        pid = it.get("_persisted_parent_conv_id")
        it["parent_conv_id"] = pid if (pid is not None and pid != it["id"] and pid in by_id) else None
        it["children_stats"] = {"active": 0, "stuck": 0, "resolved": 0}

    def _is_resolved(it: dict) -> bool:
        # Resolved = a child that has finished its turn and isn't doing
        # anything now. Criteria:
        #   - awaiting_human / is_running / is_stuck → still active
        #   - task in a terminal status (done/halt/human_review) → resolved
        #   - task_not_current_agent (D-79) → phase finished, resolved
        #   - idle child (parent_conv_id != null) → resolved
        if it.get("awaiting_human"): return False
        if it.get("is_running"): return False
        if it.get("is_stuck"): return False
        task = it.get("task")
        if task and task.get("status") in ("done", "halt", "human_review"):
            return True
        if it.get("task_not_current_agent"):
            return True
        if it["parent_conv_id"] is not None:
            return True
        return False

    # 1-level aggregate: each child contributes to its parent's children_stats.
    for child in items:
        pid = child["parent_conv_id"]
        if pid is None:
            continue
        parent = by_id.get(pid)
        if parent is None:
            continue
        stats = parent["children_stats"]
        if _is_resolved(child):
            stats["resolved"] += 1
        else:
            stats["active"] += 1
        if child.get("is_stuck"):
            stats["stuck"] += 1


async def delete_conversation(conv_id: int, principal: Principal = Depends(get_principal)):
    """Permanent delete. Cascade takes care of messages/attachments/pending_asks/
    closed_conversations/read_cursors (see 001_init.sql FKs ON DELETE CASCADE).

    No admin gate: today every logged-in user is effectively an admin. Logged in
    QUESTIONS as a future improvement (admin flag/column)."""
    if principal.user_id is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    result = await db.execute(
        "DELETE FROM messaging.conversations WHERE id = $1",
        conv_id,
    )
    return {"ok": True, "result": result}


async def close_conversation(conv_id: int, principal: Principal = Depends(get_principal)):
    if principal.user_id is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    # DO UPDATE to refresh `closed_at` on repeated closes. Combined with the
    # `closed_at >= last_message_at` rule (D-28), ensures that closing a
    # conv that was closed before + reopened by a new msg hides it again.
    await db.execute(
        """INSERT INTO web.closed_conversations (conversation_id, user_id, closed_at)
           VALUES ($1, $2, now())
           ON CONFLICT (conversation_id, user_id)
           DO UPDATE SET closed_at = EXCLUDED.closed_at""",
        conv_id, principal.user_id,
    )
    return {"ok": True}


async def reopen_conversation(conv_id: int, principal: Principal = Depends(get_principal)):
    if principal.user_id is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    await db.execute(
        "DELETE FROM web.closed_conversations WHERE conversation_id = $1 AND user_id = $2",
        conv_id, principal.user_id,
    )
    return {"ok": True}


async def close_all_conversations(principal: Principal = Depends(get_principal)):
    """Marks ALL of the user's visible conversations as closed, except those with a pending ask."""
    if principal.user_id is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    # DO UPDATE refreshes `closed_at` (see close_conversation) to honor the
    # `closed_at >= last_message_at` rule.
    result = await db.execute(
        """
        INSERT INTO web.closed_conversations (conversation_id, user_id, closed_at)
        SELECT c.id, $1, now() FROM messaging.conversations c
         WHERE NOT EXISTS (
                 -- D-111: the human is only blocked from closing by ask_human.
                 -- ask_agent doesn't prevent close (cross-agent, silent).
                 SELECT 1 FROM messaging.pending_asks pa
                  WHERE pa.conversation_id = c.id AND pa.resolved_at IS NULL
                    AND pa.kind = 'ask_human'
               )
        ON CONFLICT (conversation_id, user_id)
        DO UPDATE SET closed_at = EXCLUDED.closed_at
        """,
        principal.user_id,
    )
    return {"ok": True, "result": result}


# ---------- Scheduler proxy (PWA <-> scheduler container) ----------
#
# The scheduler exposes its own mini HTTP API (orchestrator/scheduler_http.py)
# on the compose network, without auth — the port isn't published on the host. Here in the broker
# we proxy it with auth for the UI. Read-only via a normal session; actions
# (run/pause/resume) require admin.

SCHEDULER_URL = os.environ.get("SCHEDULER_URL", "http://scheduler:8811").rstrip("/")


async def _scheduler_request(method: str, path: str, *, timeout: float = 10.0) -> tuple[int, Any]:
    """Makes a request to the scheduler and returns (status, parsed_body). body may be
    dict, list or None (204)."""
    url = f"{SCHEDULER_URL}{path}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as sess:
            async with sess.request(method, url) as r:
                if r.status == 204:
                    return r.status, None
                ct = (r.headers.get("Content-Type") or "").lower()
                if "json" in ct:
                    return r.status, await r.json()
                return r.status, {"raw": (await r.text())[:500]}
    except aiohttp.ClientError as e:
        raise HTTPException(status_code=502, detail=f"scheduler unreachable: {e}")
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="scheduler timeout")


@router.get("/scheduler/health")
async def scheduler_health(_: Principal = Depends(get_principal)):
    """Aggregated scheduler state (uptime, counters). Proxy of :8811/health."""
    status, body = await _scheduler_request("GET", "/health")
    if status >= 500:
        raise HTTPException(status_code=status, detail=body)
    return body


@router.get("/scheduler/jobs")
async def scheduler_jobs(_: Principal = Depends(get_principal)):
    """Lists registered jobs with next_run_time, pause status, last run."""
    status, body = await _scheduler_request("GET", "/jobs")
    if status >= 400:
        raise HTTPException(status_code=status, detail=body)
    return body


@router.post("/scheduler/jobs/{job_id}/run")
async def scheduler_run_job(job_id: str, _: Principal = Depends(require_admin)):
    """Fires dispatch_job(job) ad hoc in the scheduler. Returns 202 immediately; the
    job runs in the background (backups take a few minutes)."""
    status, body = await _scheduler_request("POST", f"/jobs/{job_id}/run", timeout=15.0)
    if status >= 400:
        raise HTTPException(status_code=status, detail=body)
    return body


@router.post("/scheduler/jobs/{job_id}/pause")
async def scheduler_pause_job(job_id: str, _: Principal = Depends(require_admin)):
    """Pauses a job — runtime-only, lost when the scheduler restarts."""
    status, body = await _scheduler_request("POST", f"/jobs/{job_id}/pause")
    if status >= 400:
        raise HTTPException(status_code=status, detail=body or "pause failed")
    return {"ok": True}


@router.post("/scheduler/jobs/{job_id}/resume")
async def scheduler_resume_job(job_id: str, _: Principal = Depends(require_admin)):
    """Resumes a paused job."""
    status, body = await _scheduler_request("POST", f"/jobs/{job_id}/resume")
    if status >= 400:
        raise HTTPException(status_code=status, detail=body or "resume failed")
    return {"ok": True}
