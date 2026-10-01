"""Internal client for the HTTP broker.

- Sending messages: POST /api/messages on the broker (HTTP).
- Receiving messages: direct Postgres LISTEN (a trigger fires pg_notify on
  every INSERT into messaging.messages).
- Events are converted to a standard shape (dict with type, sender_id,
  sender_full_name, sender_email, subject, display_recipient, content, id)
  consumed by the Dispatcher/SessionManager.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, AsyncIterator

import aiohttp
import asyncpg

from .log import get_logger


log = get_logger(__name__)


# Linux filesystem caps filename at 255 bytes. Reserve headroom for suffixes the
# framework appends (e.g. `.session_state.json`) and unicode multibyte chars.
SLUG_MAX_BYTES = 200


@dataclass(frozen=True)
class TopicKey:
    stream: str
    topic: str

    def slug(self) -> str:
        def safe(s: str) -> str:
            return "".join(c if c.isalnum() or c in "-_" else "_" for c in s).strip("_")
        stream_safe = safe(self.stream)
        topic_safe = safe(self.topic)
        full = f"{stream_safe}__{topic_safe}"
        if len(full.encode("utf-8")) <= SLUG_MAX_BYTES:
            return full
        # Topic too long (e.g. a whole description passed as next_topic).
        # Truncate byte-safely and append a short hash to keep the slug deterministic and unique.
        digest = hashlib.sha1(self.topic.encode("utf-8")).hexdigest()[:10]
        # Reserved: stream_safe + "__" + "__" + digest
        reserved = len(stream_safe.encode("utf-8")) + 2 + 2 + len(digest)
        keep_bytes = max(10, SLUG_MAX_BYTES - reserved)
        topic_bytes = topic_safe.encode("utf-8")[:keep_bytes]
        # Drop a trailing partial multibyte char (decode with 'ignore' cuts at the right point)
        topic_trunc = topic_bytes.decode("utf-8", errors="ignore").rstrip("_")
        return f"{stream_safe}__{topic_trunc}__{digest}"


class InternalClient:
    """HTTP client for the internal broker (sending + LISTEN/NOTIFY for events)."""

    def __init__(self, broker_url: str, token: str, streams: list[str],
                 database_url: str | None = None):
        self._broker = broker_url.rstrip("/")
        self._token = token
        self._streams = streams
        self._database_url = database_url or os.environ["DATABASE_URL"]
        self._profile: dict[str, Any] = {}
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._stream_id_by_name: dict[str, int] = {}
        self._subscribed_convs: set[int] = set()
        self._listen_conn: asyncpg.Connection | None = None
        # Serializes calls on `_listen_conn` — asyncpg does not allow
        # concurrent operations on the same connection (stmt_exclusive_section).
        # Without this lock, N parallel `add_listener` calls (e.g. ask_agents_many)
        # fail all but one with InterfaceError, leaving the asking agent
        # deaf to the replies on those convs.
        self._listen_lock = asyncio.Lock()
        self._listener_task: asyncio.Task | None = None
        self._http: aiohttp.ClientSession | None = None
        self._stopped = asyncio.Event()

    @property
    def owned_streams(self) -> list[str]:
        """Streams this agent subscribes to (its event sources).
        Used by the dispatcher to filter events that arrive via
        `subscribe_to_conversation` but belong to a child conv in another
        stream — those must only unblock ask_agent/ask_human, not
        become events queued in the local topic loop (D-75)."""
        return list(self._streams)

    @property
    def user_id(self) -> int:
        return int(self._profile.get("user_id", 0))

    @property
    def email(self) -> str:
        return str(self._profile.get("email", ""))

    @property
    def full_name(self) -> str:
        return str(self._profile.get("full_name", self._profile.get("username", "")))

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def start(self) -> None:
        self._http = aiohttp.ClientSession(headers=self._headers)
        # /api/users/me
        async with self._http.get(f"{self._broker}/api/users/me") as r:
            r.raise_for_status()
            me = await r.json()
            self._profile = {
                "user_id": me.get("user_id"),
                "email": me.get("username") + "@internal.ai-company",
                "full_name": me.get("username"),
                "username": me.get("username"),
            }
        # stream IDs (for the LISTEN channels)
        async with self._http.get(f"{self._broker}/api/streams") as r:
            r.raise_for_status()
            resp = await r.json()
            streams = resp.get("streams", []) if isinstance(resp, dict) else resp
            for s in streams:
                self._stream_id_by_name[s["name"]] = s["id"]
        # Unread catch-up (D-52) BEFORE LISTEN. For each stream, fetch the
        # persisted cursor (last_read_message_id) and replay missing msgs
        # from the broker. Then enable LISTEN — the window between the last
        # replay and add_listener is negligible (one cursor UPDATE + add_listener).
        # pg_notify would not be delivered while LISTEN is not active, so
        # new msgs in that window also get replayed next time.
        await self._catch_up_unread()

        # Start LISTEN on each configured stream
        self._listen_conn = await asyncpg.connect(self._database_url)
        for stream in self._streams:
            sid = self._stream_id_by_name.get(stream)
            if sid is None:
                log.warning("internal_client.stream_not_found", stream=stream)
                continue
            await self._listen_conn.add_listener(f"msg_stream_{sid}", self._on_notify)
        # LISTEN on the global `agent_ctrl` channel for control events (cancel etc).
        # Payload is JSON with `stream` — `_on_ctrl_notify` filters to the
        # streams this agent listens to.
        await self._listen_conn.add_listener("agent_ctrl", self._on_ctrl_notify)
        log.info(
            "internal_client.started",
            user_id=self.user_id, email=self.email,
            streams=[(s, self._stream_id_by_name.get(s)) for s in self._streams],
        )

    async def _catch_up_unread(self) -> None:
        """Replays messages that arrived while the bot was down.

        For each subscribed stream, fetch the cursor (`GET /api/subscriptions/cursor`)
        and request msgs with `id > last_read` via `GET /api/messages?stream&since_id`.
        Each msg becomes an event in the same shape as the LISTEN path and enters the pipeline.
        The cursor advances at the end of each stream's replay.

        On first boot (cursor NULL), we set it to the stream's current max(id)
        WITHOUT replaying — avoids an avalanche replaying the whole history at
        install time. Real catch-up happens from the 2nd boot onward.
        """
        if self._http is None:
            return
        for stream in self._streams:
            sid = self._stream_id_by_name.get(stream)
            if sid is None:
                continue
            try:
                async with self._http.get(
                    f"{self._broker}/api/subscriptions/cursor",
                    params={"stream": stream},
                ) as r:
                    if r.status == 404:
                        # subscription does not exist yet — reconcile creates it, but
                        # in a boot race it may not have been applied. Skip; next
                        # time picks it up.
                        log.warning("internal_client.cursor_sub_missing", stream=stream)
                        continue
                    r.raise_for_status()
                    cursor_body = await r.json()
                    last_read = cursor_body.get("last_read_message_id")
            except Exception:
                log.exception("internal_client.cursor_fetch_failed", stream=stream)
                continue

            if last_read is None:
                # First boot: seed the cursor with the stream's last msg
                # and do not replay. Avoids an avalanche of N months of history.
                max_id = await self._fetch_max_stream_id(stream)
                if max_id > 0:
                    await self._save_cursor(stream, max_id)
                log.info("internal_client.cursor_seeded", stream=stream, last_read=max_id)
                continue

            # Replay: page until empty. Limit 500 per request; loop advances.
            replayed = 0
            page_cursor = last_read
            while True:
                try:
                    async with self._http.get(
                        f"{self._broker}/api/messages",
                        params={"stream": stream, "since_id": page_cursor, "limit": 500},
                    ) as r:
                        r.raise_for_status()
                        rows = await r.json()
                except Exception:
                    log.exception("internal_client.replay_fetch_failed", stream=stream)
                    break
                if not rows:
                    break
                for m in rows:
                    # Skip own msgs (the bot does not consume what it posted itself)
                    if m.get("sender_id") == self.user_id:
                        page_cursor = max(page_cursor, m["id"])
                        continue
                    # Migration 026: catch-up also skips echoes (same
                    # rationale as _on_notify) — the cursor advances to avoid
                    # a replay loop.
                    if m.get("kind", "regular") != "regular":
                        page_cursor = max(page_cursor, m["id"])
                        continue
                    event = self._event_from_message(m)
                    await self._queue.put(event)
                    replayed += 1
                    page_cursor = max(page_cursor, m["id"])
                if len(rows) < 500:
                    break
            if page_cursor > last_read:
                await self._save_cursor(stream, page_cursor)
            if replayed:
                log.info(
                    "internal_client.catch_up_replayed",
                    stream=stream, replayed=replayed, cursor=page_cursor,
                )

    async def _fetch_max_stream_id(self, stream: str) -> int:
        """Finds the stream's max(id) by querying /api/messages with since=0 limit=1
        ordered DESC (the endpoint returns ASC, so we loop with jumps).
        Simple solution: fetch limit=1 starting at since_id=0 and assume 1 page;
        for a stream with a large history, seeding with max=0 is also acceptable
        (it only means everything is replayed on the next start — which is the
        behavior we want to avoid; rare in production because seeding
        happens right at install time). Trade-off accepted to avoid a new
        endpoint just for this.
        """
        if self._http is None:
            return 0
        # Optimistic bound: fetch 500 msgs and take the max id. On first boot
        # this is 0 (empty stream) or <=500. Rarely more.
        try:
            async with self._http.get(
                f"{self._broker}/api/messages",
                params={"stream": stream, "since_id": 0, "limit": 500},
            ) as r:
                if r.status != 200:
                    return 0
                rows = await r.json()
        except Exception:
            return 0
        if not rows:
            return 0
        return max(m["id"] for m in rows)

    async def _save_cursor(self, stream: str, message_id: int) -> None:
        if self._http is None:
            return
        try:
            async with self._http.post(
                f"{self._broker}/api/subscriptions/cursor",
                json={"stream": stream, "last_read_message_id": message_id},
            ) as r:
                if r.status >= 400:
                    log.warning(
                        "internal_client.cursor_save_failed",
                        stream=stream, message_id=message_id, status=r.status,
                    )
        except Exception:
            log.exception("internal_client.cursor_save_exc", stream=stream)

    def mark_processed(self, stream: str, message_id: int) -> None:
        """Advances the cursor to `message_id` after the dispatcher has finished
        processing that msg's turn (D-78). Fire-and-forget.

        If called several times for the same stream, the broker does
        `GREATEST(current, new)` — the cursor never goes back. If the stream is
        not one this agent subscribes to, no-op.
        """
        if not stream or stream not in self._streams:
            return
        asyncio.create_task(self._save_cursor(stream, message_id))

    def _event_from_message(self, m: dict) -> dict:
        """Converts a /api/messages row into the Dispatcher event shape.
        Same shape _enrich_and_enqueue builds for notifies — centralized
        here so catch-up can reuse it.

        D-87: includes `conversation_id` so claude_runner.handle can
        propagate it to the McpBroker via register_topic — needed by the
        ask_agent MCP handler to pass parent_conv_id when creating the child conv.
        """
        return {
            "id": m["id"],
            "conversation_id": m.get("conversation_id"),
            "type": "stream",
            "sender_id": m["sender_id"],
            "sender_full_name": m["sender_username"],
            "sender_email": f"{m['sender_username']}@internal.ai-company",
            "sender_is_bot": False,
            "subject": m["topic"],
            "display_recipient": m["stream"],
            "content": m["content"],
            "timestamp": m["sent_at"],
        }

    def stop(self) -> None:
        self._stopped.set()

    def _on_notify(self, _conn, _pid, _channel: str, payload: str) -> None:
        """LISTEN callback — runs in the asyncpg loop. Enriches and enqueues."""
        try:
            data = json.loads(payload)
            # Filter: skip own msgs
            if data.get("sender_id") == self.user_id:
                return
            # Migration 026: skip kind != 'regular' — echoes (D-100 forward
            # of a child-conv reply) are purely visual and do not wake the
            # runner. Without this, the parent agent got a redundant extra
            # turn every time a child agent replied (case observed on
            # 2026-04-28: the parent wrote "three options" and 13s later
            # woke up to write "I already presented the three options" —
            # triggered by the echo of the child's reply).
            if data.get("kind", "regular") != "regular":
                return
            asyncio.create_task(self._enrich_and_enqueue(data))
        except Exception:
            log.exception("internal_client.notify_parse_failed", payload=payload[:200])

    def _on_ctrl_notify(self, _conn, _pid, _channel: str, payload: str) -> None:
        """`agent_ctrl` LISTEN callback — framework control events
        (cancel_topic etc). Filters by the streams the agent listens to."""
        try:
            data = json.loads(payload)
            stream = data.get("stream")
            if stream not in self._streams:
                return
            ctrl_event = {
                "_ctrl": True,
                "ctrl_type": data.get("type"),
                "display_recipient": stream,
                "subject": data.get("topic"),
                "user_id": data.get("user_id"),
                "silent": bool(data.get("silent", False)),
            }
            # Enqueue directly (no enrichment — the payload has everything).
            self._queue.put_nowait(ctrl_event)
        except Exception:
            log.exception("internal_client.ctrl_notify_failed", payload=payload[:200])

    async def _enrich_and_enqueue(self, data: dict) -> None:
        """Fetches stream/topic/sender metadata via HTTP (cache-friendly in the future) and
        converts it to the standard event shape consumed by the Dispatcher."""
        if self._http is None:
            return
        conv_id = data["conversation_id"]
        async with self._http.get(
            f"{self._broker}/api/messages",
            params={"conversation_id": conv_id, "since_id": data["id"] - 1, "limit": 1},
        ) as r:
            if r.status != 200:
                log.warning("internal_client.enrich_failed", status=r.status)
                return
            rows = await r.json()
            if not rows:
                return
            m = rows[0]
        event = self._event_from_message(m)
        await self._queue.put(event)
        # D-78: the cursor does NOT advance here (pre-D-78 it did, to skip replay of
        # already-queued msgs). Problem: if the container died with a msg in the
        # in-memory queue, the cursor was ahead → catch-up on the next start
        # did not replay it → msg lost. Now the dispatcher calls
        # `mark_processed` after handler.handle finishes (in _run_turn),
        # so it only advances after the turn completes. A duplicate replay
        # on restart is tolerated: the dispatcher re-enqueues, claude_runner
        # resumes by session_id, Claude ignores or reprocesses — better than
        # losing the msg.

    async def subscribe_to_conversation(self, conversation_id: int) -> bool:
        """LISTEN on the `msg_conv_<conversation_id>` channel — only receives
        messages from that specific conversation. Used by ask_agent to wait for
        the target's reply WITHOUT over-subscribing to the whole stream (bug D-50).
        Idempotent: no-op if already subscribed.
        """
        if self._listen_conn is None:
            return False
        if conversation_id in self._subscribed_convs:
            return True
        try:
            async with self._listen_lock:
                # Re-check inside the lock — avoids a double add when two
                # calls with the same conv_id race in parallel.
                if conversation_id in self._subscribed_convs:
                    return True
                await self._listen_conn.add_listener(
                    f"msg_conv_{conversation_id}", self._on_notify
                )
                self._subscribed_convs.add(conversation_id)
            log.info("internal_client.subscribed_conv", conversation_id=conversation_id)
            return True
        except Exception:
            log.exception(
                "internal_client.subscribe_conv_failed", conversation_id=conversation_id
            )
            return False

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while not self._stopped.is_set():
            try:
                ev = await asyncio.wait_for(self._queue.get(), timeout=5.0)
                yield ev
            except asyncio.TimeoutError:
                continue

    async def send_message(
        self,
        stream: str,
        topic: str,
        content: str,
        parent_conv_id: int | None = None,
        client_id: str | None = None,
        kind: str = "regular",
    ) -> dict[str, Any]:
        """D-87: `parent_conv_id` is passed to the broker, which stores it in the
        column of the same name in messaging.conversations when it creates the conv
        (first msg in (stream, topic)). The hierarchy becomes an O(1) FK lookup.

        `client_id`: idempotency key (UNIQUE per conversation_id). Allows
        safely resending the same content (race, retry, D-100 echo
        double-fire) without creating duplicate messages. Optional — None keeps
        the legacy behavior.

        `kind` (migration 026): 'regular' (default) triggers a turn in the
        agent's listener. 'echo' is a visual forward of a child-conv
        reply (D-100) — visible in the PWA but does not wake the
        parent agent's runner."""
        assert self._http is not None
        body_json: dict[str, Any] = {"stream": stream, "topic": topic, "content": content}
        if parent_conv_id is not None:
            body_json["parent_conv_id"] = parent_conv_id
        if client_id is not None:
            body_json["client_id"] = client_id
        if kind != "regular":
            body_json["kind"] = kind
        async with self._http.post(
            f"{self._broker}/api/messages",
            json=body_json,
        ) as r:
            if r.status >= 400:
                body = await r.text()
                raise RuntimeError(f"send_message failed: HTTP {r.status} {body[:200]}")
            return await r.json()

    async def update_message(self, message_id: int, content: str) -> dict[str, Any]:
        # The current broker does not support editing. No-op kept for signature
        # compatibility (callers may invoke it but it has no effect).
        log.warning("internal_client.update_message_noop", message_id=message_id)
        return {"result": "success"}

    async def archive_conversation(self, conv_id: int) -> dict[str, Any]:
        """POST /api/conversations/<id>/archive. The backend cascades to
        descendants + cancels active runners (D-72)."""
        assert self._http is not None
        async with self._http.post(
            f"{self._broker}/api/conversations/{conv_id}/archive",
        ) as r:
            if r.status >= 400:
                body = await r.text()
                raise RuntimeError(
                    f"archive_conversation failed: HTTP {r.status} {body[:200]}"
                )
            return await r.json()

    async def create_pending_ask(
        self,
        stream: str,
        topic: str,
        question: str,
        context: str = "",
        blocking: bool = True,
        kind: str = "ask_human",
        target_agent: str | None = None,
    ) -> dict[str, Any]:
        """Registers a pending_ask on the broker. Essential for ask_human/ask_agent —
        without it, the human does not see the conversation in Mine and the
        push_notifier does not fire. Called after the question's send_message.

        D-111: `kind='ask_agent'` is persisted for restart recovery (the broker
        auto-resolves it when the target replies). UI/push filter on `kind='ask_human'`
        so the human is not alerted about agent-to-agent asks.

        The response includes `resolved: bool` — if true, the ask was already answered
        (restart-recovery case where the target replied before the asker came back);
        the caller reads `answer_message_id` to fetch the answer directly."""
        assert self._http is not None
        async with self._http.post(
            f"{self._broker}/api/asks",
            json={
                "stream": stream,
                "topic": topic,
                "question": question,
                "context": context,
                "blocking": blocking,
                "kind": kind,
                "target_agent": target_agent,
            },
        ) as r:
            if r.status >= 400:
                body = await r.text()
                raise RuntimeError(f"create_pending_ask failed: HTTP {r.status} {body[:200]}")
            return await r.json()
