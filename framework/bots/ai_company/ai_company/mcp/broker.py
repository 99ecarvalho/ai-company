"""McpBroker — state shared between the MCP server and the BaseAgent.

Responsibilities:
  - Keep a topic_slug -> TopicKey dict (the MCP resolves URLs by slug)
  - Manage an asyncio.Future per topic_key for pending asks
  - Persist questions in `pending_questions/<slug>.json` for crash recovery
  - Expose callbacks the BaseAgent uses to post questions to the broker
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from ..internal_client import TopicKey
from ..log import get_logger

log = get_logger(__name__)

# callback: (topic, question, context, blocking) -> awaitable of None
OnAskCallback = Callable[[TopicKey, str, str, bool], Awaitable[None]]

# callback for notify_human: (topic, message) -> awaitable. Posts a msg to the
# broker without creating a pending_ask. Unlike ask_human it does not
# wait, does not block, and does not become a waiting badge in the UI.
OnNotifyCallback = Callable[[TopicKey, str], Awaitable[None]]

# callback for archive_conversation: (conv_id) -> awaitable. POSTs
# /api/conversations/<id>/archive to the broker. Errors must propagate as
# exceptions so the MCP handler can render them for the agent.
OnArchiveCallback = Callable[[int], Awaitable[None]]

# callback for ask_agent: (asker_topic, from_agent, target_agent, question, context)
# -> tuple (TopicKey, answer_if_already_resolved).
#   - TopicKey: target where the msg was posted (or found again).
#   - answer_if_already_resolved: D-111 restart recovery — if this conv's
#     pending_ask was already resolved by a target reply while the
#     asker was down, returns the content directly (no repost, no
#     wait). None = normal path (caller awaits the Future).
# asker_topic is the caller's current topic (None if not mappable) —
# used for the cycle/depth check when building the target topic name.
OnAskAgentCallback = Callable[
    [TopicKey | None, str, str, str, str], Awaitable[tuple[TopicKey, str | None]]
]


class McpBroker:
    def __init__(self, pending_dir: Path):
        self.pending_dir = pending_dir
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self._pending_futures: dict[TopicKey, asyncio.Future[str]] = {}
        self._slug_to_key: dict[str, TopicKey] = {}
        # D-87: conv_id indexed by slug. Populated by the Dispatcher as it
        # processes each event. Used by the `__ask_agent` MCP handler to
        # pass parent_conv_id to the broker when creating the new `__ask-from-*` conv.
        self._slug_to_conv_id: dict[str, int] = {}
        self._on_ask: OnAskCallback | None = None
        self._on_ask_agent: OnAskAgentCallback | None = None
        self._on_notify: OnNotifyCallback | None = None
        self._on_archive: OnArchiveCallback | None = None
        # Optional callback: given a TopicKey, returns True if that topic
        # has a pending (unresolved) ask_human. Used for ask_agent's adaptive
        # timeout — if the target is blocked on a human, the wait is
        # extended indefinitely instead of giving up.
        self._check_target_blocked_on_human: Callable[[TopicKey], Awaitable[bool]] | None = None

    # ---------- topic <-> slug registry ----------

    def register_topic(self, key: TopicKey, conv_id: int | None = None) -> str:
        slug = key.slug()
        self._slug_to_key[slug] = key
        if conv_id is not None:
            self._slug_to_conv_id[slug] = conv_id
        return slug

    def topic_for_slug(self, slug: str) -> TopicKey | None:
        return self._slug_to_key.get(slug)

    def conv_id_for_slug(self, slug: str) -> int | None:
        """D-87: returns the conv_id registered via register_topic. Used by the
        `__ask_agent`/`__ask_agents_many` MCP handlers to pass
        parent_conv_id when creating the new `__ask-from-*` conv."""
        return self._slug_to_conv_id.get(slug)

    # ---------- callbacks for posting to the broker ----------

    def set_on_ask(self, cb: OnAskCallback) -> None:
        self._on_ask = cb

    def set_on_ask_agent(self, cb: OnAskAgentCallback) -> None:
        self._on_ask_agent = cb

    def set_on_notify(self, cb: OnNotifyCallback) -> None:
        self._on_notify = cb

    async def notify_human(self, key: TopicKey, message: str) -> None:
        """Posts a plain message to the topic without creating a pending_ask."""
        if self._on_notify is None:
            raise RuntimeError("notify_human is not configured on this agent")
        log.info("broker.notify_human", topic=key.slug(), length=len(message))
        await self._on_notify(key, message)

    def set_on_archive(self, cb: OnArchiveCallback) -> None:
        self._on_archive = cb

    async def archive_conversation(self, conv_id: int) -> None:
        if self._on_archive is None:
            raise RuntimeError("archive_conversation is not configured on this agent")
        log.info("broker.archive_conversation", conv_id=conv_id)
        await self._on_archive(conv_id)

    def set_check_target_blocked_on_human(
        self, cb: Callable[[TopicKey], Awaitable[bool]]
    ) -> None:
        self._check_target_blocked_on_human = cb

    # ---------- state ----------

    def has_pending(self, key: TopicKey) -> bool:
        fut = self._pending_futures.get(key)
        return fut is not None and not fut.done()

    def pending_question_path(self, key: TopicKey) -> Path:
        return self.pending_dir / f"{key.slug()}.json"

    # ---------- main API ----------

    async def ask_human(
        self,
        key: TopicKey,
        question: str,
        context: str = "",
    ) -> str:
        """Invoked by the MCP server when claude calls the ask_human tool.

        Always blocks indefinitely until the human answers. No timeout,
        no fallback — whoever cannot wait uses `complete_phase(next='halt')`.
        Decision made after an incident where a timeout left an orphan
        `pending_ask` in the DB while the agent moved on via fallback,
        confusing the human (permanent NEEDS YOU badge on a conv that was no
        longer listening for an answer)."""
        asked_iso = datetime.now(tz=timezone.utc).isoformat()
        pending_path = self.pending_question_path(key)
        pending_path.write_text(
            json.dumps(
                {
                    "stream": key.stream,
                    "topic": key.topic,
                    "question": question,
                    "context": context,
                    "asked_at": asked_iso,
                    "asked_at_epoch": time.time(),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        log.info(
            "broker.ask_human",
            topic=key.slug(),
            question_preview=question[:120],
        )

        # Notify the human (post to the broker)
        if self._on_ask is not None:
            try:
                await self._on_ask(key, question, context, True)
            except Exception:
                log.exception("broker.on_ask_failed", topic=key.slug())

        # Indefinite blocking: await the Future until someone (human via PWA →
        # broker → Dispatcher → broker.resolve()) sets the result.
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._pending_futures[key] = fut
        try:
            response = await fut
            log.info("broker.ask_human.resolved", topic=key.slug(), length=len(response))
            return response
        finally:
            self._pending_futures.pop(key, None)
            try:
                pending_path.unlink(missing_ok=True)
            except OSError:
                pass

    def resolve(self, key: TopicKey, response: str) -> bool:
        """Called by the Dispatcher when a msg arrives on a topic with a pending question."""
        fut = self._pending_futures.get(key)
        if fut is None or fut.done():
            return False
        fut.set_result(response)
        log.info("broker.resolved", topic=key.slug(), length=len(response))
        return True

    # ---------- ask_agent (agent-to-agent communication) ----------

    async def ask_agent_via_callback(
        self,
        *,
        asker_topic: TopicKey | None,
        from_agent: str,
        target_agent: str,
        question: str,
        context: str,
        timeout_minutes: float,
    ) -> str:
        """Full helper: calls on_ask_agent to post the msg (which returns the
        created TopicKey), registers a Future, awaits resolve/timeout."""
        if self._on_ask_agent is None:
            return "[error] ask_agent is not configured on this agent."
        try:
            target_key, already_resolved = await self._on_ask_agent(
                asker_topic, from_agent, target_agent, question, context
            )
        except Exception as e:
            log.exception("broker.ask_agent.post_failed", target=target_agent)
            return f"[error asking] {e}"

        # D-111 restart recovery: the target already replied while the asker was
        # down. Return directly without registering a Future or waiting.
        if already_resolved is not None:
            log.info(
                "broker.ask_agent.restart_recovered",
                target=target_key.slug(),
                length=len(already_resolved),
            )
            return already_resolved

        log.info(
            "broker.ask_agent",
            target=target_key.slug(),
            timeout_minutes=timeout_minutes,
            question_preview=question[:120],
        )
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._pending_futures[target_key] = fut
        try:
            # Loop with extension: on timeout, check whether the target
            # is blocked on an ask_human. If so, restart the wait —
            # indefinitely, until the human answers and the target unblocks.
            # If the target has no pending ask_human and did not reply, real timeout.
            interval = timeout_minutes * 60
            extensions = 0
            while True:
                done, _ = await asyncio.wait({fut}, timeout=interval)
                if fut in done:
                    response = fut.result()
                    log.info(
                        "broker.ask_agent.resolved",
                        target=target_key.slug(),
                        length=len(response),
                        extensions=extensions,
                    )
                    return response
                blocked = False
                if self._check_target_blocked_on_human is not None:
                    try:
                        blocked = await self._check_target_blocked_on_human(target_key)
                    except Exception:
                        log.exception(
                            "broker.ask_agent.check_blocked_failed",
                            target=target_key.slug(),
                        )
                        blocked = False
                if blocked:
                    extensions += 1
                    log.info(
                        "broker.ask_agent.extend_for_human_wait",
                        target=target_key.slug(),
                        extensions=extensions,
                    )
                    continue
                log.warning(
                    "broker.ask_agent.timeout",
                    target=target_key.slug(),
                    extensions=extensions,
                )
                if not fut.done():
                    fut.cancel()
                return f"[timeout] Agent {target_agent} did not reply within {timeout_minutes}min."
        finally:
            self._pending_futures.pop(target_key, None)

    async def ask_agents_many(
        self,
        *,
        asker_topic: TopicKey | None,
        from_agent: str,
        asks: list[dict[str, str]],
        timeout_minutes: float,
    ) -> list[dict[str, str]]:
        """Runs N ask_agent_via_callback calls concurrently via asyncio.gather.

        Each item in `asks` is a dict with `target_agent`, `question`, and
        optionally `context`. Returns a list in the same order with `response`
        (success) or `error` (policy/cycle/depth/post/timeout), without failing
        the whole batch because of one item.
        """
        log.info(
            "broker.ask_agents_many.start",
            count=len(asks),
            targets=[a.get("target_agent") for a in asks],
            timeout_minutes=timeout_minutes,
        )

        async def _one(ask: dict[str, str]) -> str:
            return await self.ask_agent_via_callback(
                asker_topic=asker_topic,
                from_agent=from_agent,
                target_agent=ask["target_agent"],
                question=ask["question"],
                context=ask.get("context") or "",
                timeout_minutes=timeout_minutes,
            )

        results = await asyncio.gather(
            *(_one(a) for a in asks), return_exceptions=True
        )
        out: list[dict[str, str]] = []
        for ask, res in zip(asks, results):
            item: dict[str, str] = {
                "target_agent": ask["target_agent"],
                "question": ask["question"],
            }
            if isinstance(res, BaseException):
                item["error"] = f"{type(res).__name__}: {res}"
            else:
                item["response"] = res
            out.append(item)
        log.info(
            "broker.ask_agents_many.done",
            count=len(out),
            errors=sum(1 for o in out if "error" in o),
        )
        return out
