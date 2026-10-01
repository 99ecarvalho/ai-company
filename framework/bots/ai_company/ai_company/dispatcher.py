"""Dispatcher: 1 topic = 1 dedicated worker, 1 queue per topic.

Guarantees:
  - Consecutive messages in the same topic go to the same worker, in order
  - `pool_size` is TURN-LEVEL (D-27): it limits concurrent Claude invocations,
    not "live" topics. The slot is only held during `handler.handle()`.
  - The topic loop persists across turns to keep the topic's workdir and
    session_id "warm" — it is released only after `idle_timeout_sec` without msgs.
  - If the pool is full at the start of a turn, posts an ack in the chat before waiting.
  - Pool contention > CONTENTION_LOG_THRESHOLD_SEC is logged as `pool.contention`.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Protocol


from .log import get_logger
from .mcp.broker import McpBroker
from .session_manager import SessionManager
from .worker_pool import WorkerPool
from .internal_client import TopicKey, InternalClient as BrokerClient

log = get_logger(__name__)

MENTION_RE = re.compile(r"@\*\*[^*]+\*\*\s*")

# Log `pool.contention` when a turn waits longer than this to acquire the slot.
CONTENTION_LOG_THRESHOLD_SEC = 2.0


class EventHandler(Protocol):
    """Protocol: something that can process an event in a given cwd.
    Implemented by ClaudeRunner or by mocks in the tests.
    """
    async def handle(self, event: dict[str, Any], topic_key: TopicKey, workdir) -> None: ...


class Dispatcher:
    def __init__(
        self,
        pool: WorkerPool,
        session_mgr: SessionManager,
        handler: EventHandler,
        broker_client: BrokerClient | None = None,
        broker: McpBroker | None = None,
        idle_timeout_sec: int = 900,
        audio_transcriber=None,  # kept for compat; no-op (the PWA transcribes)
    ):
        self.pool = pool
        self.session_mgr = session_mgr
        self.handler = handler
        self.broker_client = broker_client
        self.broker = broker
        self.idle_timeout_sec = idle_timeout_sec
        self._queues: dict[TopicKey, asyncio.Queue[dict]] = {}
        self._tasks: dict[TopicKey, asyncio.Task] = {}
        self._running_handler: set[TopicKey] = set()
        # D-71: the active handler stores the Claude CLI proc here so a turn
        # can be cancelled mid-flight (SIGTERM + SIGKILL fallback). Slug → Process.
        # Lifetime == duration of the subprocess call in claude_runner;
        # register/unregister comes from the runner itself via handler_register_proc.
        self._topic_procs: dict[str, asyncio.subprocess.Process] = {}
        # D-72: when the human invokes cancel_topic and SIGTERM succeeds,
        # we mark the slug here. The Claude runner checks it via
        # `consume_user_cancel` in the retry loop — if set, it is treated as
        # non-retriable (avoids auto-resume on rc=143). Consume clears it.
        self._user_cancelled: set[str] = set()
        self._lock = asyncio.Lock()

    def consume_user_cancel(self, key: TopicKey) -> bool:
        """Check-and-clear: True if the human cancelled this topic since
        the last check. Used by the runner to avoid retrying after a
        SIGTERM coming from cancel_topic (D-72)."""
        slug = key.slug()
        if slug in self._user_cancelled:
            self._user_cancelled.discard(slug)
            return True
        return False

    def handler_register_proc(self, key: TopicKey, proc: asyncio.subprocess.Process) -> None:
        """Called by the handler (ClaudeRunner) when spawning the CLI. D-71.

        Keeps a ref to the process per topic so cancel_topic can SIGTERM it
        while the handler is already running (pre-D-71 answered "too late").
        """
        self._topic_procs[key.slug()] = proc

    def handler_unregister_proc(self, key: TopicKey) -> None:
        """Called by the handler when the CLI exits (finally after proc.wait).

        Idempotent — pop without KeyError for races with cancel.
        """
        self._topic_procs.pop(key.slug(), None)

    async def _maybe_transcribe_audio(self, event: dict[str, Any], key: TopicKey) -> None:
        return  # no-op — the PWA transcribes locally via /api/transcribe-preview

    async def dispatch(self, event: dict[str, Any]) -> None:
        # Control events (cancel, etc) come from the `agent_ctrl` channel via
        # InternalClient and are not normal messages — separate routing.
        if event.get("_ctrl"):
            await self._handle_ctrl(event)
            return

        key = TopicKey(stream=event["display_recipient"], topic=event["subject"])

        # If audio is attached, transcribe it and enrich content BEFORE any
        # routing. Also useful for ask_human: the human can answer by voice.
        await self._maybe_transcribe_audio(event, key)

        # If there is a pending ask_human OR ask_agent in this topic, the msg is the answer.
        # Resolve the Future directly (do not enqueue it for the worker). The agent's own
        # echo was already filtered upstream in internal_client._on_notify via
        # `sender_id == self.user_id` — any msg that gets here is from another
        # sender. Post-D-96 there is no ask_human in child convs anymore (gated in
        # the MCP), so children only post Claude's final answer. The emoji
        # filters `:question:`/`:loudspeaker:`/`:hourglass:` were removed.
        if self.broker is not None and self.broker.has_pending(key):
            raw_content = str(event.get("content") or "")
            content = MENTION_RE.sub("", raw_content).strip()
            if content and self.broker.resolve(key, content):
                log.info("dispatcher.routed_to_broker", topic=key.slug(), length=len(content))
                return

        # D-75: an event whose stream does NOT belong to this agent should only
        # serve to unblock ask_agent/ask_human (path above). If it got
        # here, the conv is a child (via subscribe_to_conversation)
        # and the msg is irrelevant to the agent's loop — drop it so we don't
        # create a local topic_loop in someone else's topic (it caused loops between
        # parents and children: the parent processed the child conv's msgs as its own).
        # `getattr` with a default: if broker_client does not expose
        # owned_streams (test stubs), skip the filter (backward compat).
        owned = getattr(self.broker_client, "owned_streams", None)
        if owned is not None and key.stream not in owned:
            log.info(
                "dispatcher.skip_foreign_stream",
                topic=key.slug(),
                own_streams=owned,
            )
            return

        async with self._lock:
            if key not in self._queues:
                self._queues[key] = asyncio.Queue()
                self._tasks[key] = asyncio.create_task(
                    self._topic_loop(key), name=f"topic-{key.slug()}"
                )
            await self._queues[key].put(event)
        log.debug("dispatcher.queued", topic=key.slug(), pending=self._queues[key].qsize())

    async def _topic_loop(self, key: TopicKey) -> None:
        """Topic loop: set up the workdir, consume the queue until idle timeout.

        Does NOT hold a pool slot — each turn acquires its own in `_run_turn`.
        """
        workdir = self.session_mgr.setup(key)
        log.info("topic.started", topic=key.slug(), workdir=str(workdir))
        queue = self._queues[key]
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=self.idle_timeout_sec)
                except asyncio.TimeoutError:
                    log.info("topic.idle_timeout", topic=key.slug())
                    break
                await self._run_turn(event, key, workdir)
        except asyncio.CancelledError:
            log.info("topic.cancelled", topic=key.slug())
            # Do not re-raise — we want to exit cleanly via finally
        finally:
            async with self._lock:
                # If new events arrived during shutdown, restart the loop.
                # Does not apply when we were cancelled (queue already drained by
                # `_handle_ctrl`).
                if key in self._queues and not self._queues[key].empty():
                    leftover = self._queues[key]
                    self._queues[key] = asyncio.Queue()
                    self._tasks[key] = asyncio.create_task(
                        self._topic_loop(key), name=f"topic-{key.slug()}"
                    )
                    # re-enqueue leftovers into the new queue
                    while not leftover.empty():
                        await self._queues[key].put(leftover.get_nowait())
                else:
                    self._queues.pop(key, None)
                    self._tasks.pop(key, None)
            log.info("topic.ended", topic=key.slug())

    async def _run_turn(self, event: dict[str, Any], key: TopicKey, workdir) -> None:
        """Acquire a pool slot, process 1 event, release it.

        Posts a "waiting for a slot" ack if the pool is full. Logs `pool.contention`
        when the wait exceeds `CONTENTION_LOG_THRESHOLD_SEC`.
        """
        label = f"topic={key.slug()}"
        # If the pool is full before acquire, notify in the chat.
        # Filter: no ack for internal topics (`__ask-...`) — it would add noise.
        if self.pool.free == 0 and self.broker_client is not None and not key.topic.startswith("__"):
            try:
                await self.broker_client.send_message(
                    key.stream, key.topic,
                    f"⏳ Waiting for a slot — {self.pool.in_use}/{self.pool.size} in use. "
                    f"I'll process this turn as soon as one frees up.",
                )
            except Exception:
                log.exception("dispatcher.slot_wait_notify_failed")

        t0 = asyncio.get_event_loop().time()
        async with self.pool.acquire(label=label):
            waited = asyncio.get_event_loop().time() - t0
            if waited > CONTENTION_LOG_THRESHOLD_SEC:
                log.info(
                    "pool.contention",
                    topic=key.slug(), waited_sec=round(waited, 2),
                    pool_size=self.pool.size,
                )
            self.session_mgr.touch(key)
            self._running_handler.add(key)
            try:
                await self.handler.handle(event, key, workdir)
            except Exception:
                log.exception("topic.handler_error", topic=key.slug())
            finally:
                self._running_handler.discard(key)
                # D-78: advance the cursor AFTER the turn, not on enqueue.
                # Ensures msgs that were queued but not processed (pool full
                # + container goes down) are replayed on the next start. Called even
                # if handler.handle crashed — we consider the msg "attempted",
                # otherwise it loops forever in replay (claude_runner already does
                # MAX_ATTEMPTS internal retries). Fire-and-forget; the broker uses
                # GREATEST so concurrent calls don't move the cursor backwards.
                if self.broker_client is not None:
                    stream = event.get("display_recipient")
                    msg_id = event.get("id")
                    if stream and isinstance(msg_id, int):
                        mark = getattr(self.broker_client, "mark_processed", None)
                        if callable(mark):
                            mark(stream, msg_id)

    async def _handle_ctrl(self, event: dict[str, Any]) -> None:
        """Process control events (e.g. cancel_topic) coming from the
        Postgres `agent_ctrl` channel via InternalClient."""
        ctrl_type = event.get("ctrl_type")
        stream = event.get("display_recipient")
        topic = event.get("subject")
        if not stream or not topic:
            log.warning("dispatcher.ctrl_missing_target", event=event)
            return
        key = TopicKey(stream=stream, topic=topic)

        if ctrl_type == "cancel_topic":
            await self._cancel_topic(key, silent=bool(event.get("silent", False)))
            return
        log.warning("dispatcher.unknown_ctrl_type", type=ctrl_type, topic=key.slug())

    async def _cancel_topic(self, key: TopicKey, silent: bool = False) -> None:
        """Cancel a turn — drain the queue, kill the running proc, or report a no-op.

        Flow (D-71: the kill_proc branch extends the pre-existing one):
          - No active task: nothing to cancel (info message in the chat).
          - Handler already running AND proc registered: SIGTERM the CLI + SIGKILL
            fallback after KILL_TIMEOUT_SEC. The runner returns with error_subtype
            and emits the `run_end` live_event naturally.
          - Handler already running but WITHOUT a registered proc (pre-spawn or
            post-exit): too late.
          - Task waiting on queue.get/pool.acquire: drain the queue + cancel the
            task (confirmation message in the chat).

        `silent=True` suppresses the confirmation messages in the chat. Used
        when the cancel was triggered by archiving/deleting the conv: the conv
        is already gone from the human's Active tab, so posting feedback only
        clutters the Closed tab.
        """
        too_late = False
        no_task = False
        killed_proc: asyncio.subprocess.Process | None = None
        async with self._lock:
            task = self._tasks.get(key)
            queue = self._queues.get(key)
            if task is None or task.done():
                no_task = True
            elif key in self._running_handler:
                killed_proc = self._topic_procs.get(key.slug())
                if killed_proc is None:
                    too_late = True
            else:
                # Safe to cancel: drain the queue and cancel the task.
                if queue is not None:
                    drained = 0
                    try:
                        while True:
                            queue.get_nowait()
                            drained += 1
                    except asyncio.QueueEmpty:
                        pass
                    log.info("dispatcher.cancel.queue_drained", topic=key.slug(), count=drained)
                task.cancel()

        # SIGTERM outside the lock — its proc.wait() runs in parallel
        # in the runner's loop (which holds the pool lock, not the dispatcher's).
        if killed_proc is not None:
            try:
                killed_proc.terminate()
                log.info(
                    "dispatcher.cancel.sigterm",
                    topic=key.slug(), pid=killed_proc.pid,
                )
            except ProcessLookupError:
                # Proc already exited between the check and terminate — normal race.
                log.info("dispatcher.cancel.noop_raced", topic=key.slug())
                killed_proc = None
            except Exception:
                log.exception("dispatcher.cancel.sigterm_failed", topic=key.slug())
            else:
                # D-72: mark the topic as cancelled by the human so the runner
                # doesn't treat rc=143 as a retriable external error and auto-resume.
                self._user_cancelled.add(key.slug())
                # Schedule SIGKILL as a fallback without blocking this handler.
                asyncio.create_task(self._sigkill_fallback(key, killed_proc))

        # Notifications outside the lock so we don't hold state.
        if self.broker_client is None:
            return
        if silent:
            # Cancel triggered by archive/delete: internal state was already
            # handled, we post nothing in the conv (which is going away).
            log.info(
                "dispatcher.cancel.silent",
                topic=key.slug(),
                outcome=(
                    "no_task" if no_task
                    else "too_late" if too_late
                    else "kill_requested" if killed_proc is not None
                    else "done"
                ),
            )
            return
        try:
            if no_task:
                await self.broker_client.send_message(
                    key.stream, key.topic,
                    "ℹ️ Nothing to cancel — no pending turn.",
                )
                log.info("dispatcher.cancel.no_task", topic=key.slug())
            elif too_late:
                await self.broker_client.send_message(
                    key.stream, key.topic,
                    "⚠️ Too late — Claude is already finishing this turn.",
                )
                log.info("dispatcher.cancel.too_late", topic=key.slug())
            elif killed_proc is not None:
                await self.broker_client.send_message(
                    key.stream, key.topic,
                    "🚫 Cancelled by user (SIGTERM sent to the Claude CLI).",
                )
                log.info("dispatcher.cancel.kill_requested", topic=key.slug())
            else:
                await self.broker_client.send_message(
                    key.stream, key.topic,
                    "🚫 Cancelled by user.",
                )
                log.info("dispatcher.cancel.done", topic=key.slug())
        except Exception:
            log.exception("dispatcher.cancel.notify_failed", topic=key.slug())

    async def _sigkill_fallback(
        self, key: TopicKey, proc: asyncio.subprocess.Process,
        timeout_sec: float = 3.0,
    ) -> None:
        """If SIGTERM did not bring the proc down within `timeout_sec`, send SIGKILL.

        The Claude CLI usually honors SIGTERM (flushes stream-json + emits
        `result`), but in some states (tool_use stuck on network)
        it can hang. SIGKILL guarantees the pool slot is released.
        """
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_sec)
            return  # exited cleanly
        except asyncio.TimeoutError:
            pass
        try:
            proc.kill()
            log.warning(
                "dispatcher.cancel.sigkill",
                topic=key.slug(), pid=proc.pid, waited_sec=timeout_sec,
            )
        except ProcessLookupError:
            pass
        except Exception:
            log.exception("dispatcher.cancel.sigkill_failed", topic=key.slug())
