"""Dispatcher tests — turn-level semaphore (D-27) + cancel (D-29).

Uses a mocked handler instead of real Claude (independent of CLAUDE_MOCK).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ai_company.dispatcher import Dispatcher
from ai_company.internal_client import TopicKey
from ai_company.session_manager import SessionManager
from ai_company.worker_pool import WorkerPool


class StubHandler:
    """Records processed events; optionally sleeps to simulate work."""

    def __init__(self, delay_sec: float = 0.0):
        self.delay_sec = delay_sec
        self.processed: list[tuple[TopicKey, dict]] = []
        self.handle_started = asyncio.Event()

    async def handle(self, event: dict[str, Any], topic_key: TopicKey, workdir: Path) -> None:
        self.handle_started.set()
        if self.delay_sec > 0:
            await asyncio.sleep(self.delay_sec)
        self.processed.append((topic_key, event))


class StubBrokerClient:
    """Records posted messages (queue acks, cancel confirmations, etc)."""

    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    async def send_message(self, stream: str, topic: str, content: str):
        self.sent.append((stream, topic, content))
        return {"id": len(self.sent), "stream": stream, "topic": topic, "content": content}


@pytest.fixture
def session_mgr(tmp_path: Path) -> SessionManager:
    agent_home = tmp_path / "agent"
    agent_home.mkdir()
    (agent_home / "CLAUDE.md").write_text("persona")
    (agent_home / "knowledge").mkdir()
    (tmp_path / "repos").mkdir()
    (tmp_path / "company").mkdir()
    return SessionManager(
        agent_home=agent_home,
        workspace_repos=tmp_path / "repos",
        workspace_company=tmp_path / "company",
    )


def _event(stream: str, topic: str, content: str = "hi", msg_id: int = 1) -> dict:
    return {
        "id": msg_id,
        "type": "stream",
        "sender_id": 42,
        "sender_full_name": "alice",
        "sender_email": "alice@x",
        "sender_is_bot": False,
        "subject": topic,
        "display_recipient": stream,
        "content": content,
        "timestamp": "2026-04-21T00:00:00Z",
    }


async def _wait_for(predicate, timeout: float = 2.0, interval: float = 0.01):
    """Short poll to wait for a condition in a test — avoids fixed sleeps."""
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - t0 < timeout:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# D-27: the semaphore is turn-level, not topic-level.
# ---------------------------------------------------------------------------


async def test_slot_released_between_turns(session_mgr: SessionManager):
    """With pool=1, two different topics can make progress in sequence
    without waiting for the first one's idle_timeout (old behavior)."""
    pool = WorkerPool(size=1)
    handler = StubHandler(delay_sec=0.05)
    broker_client = StubBrokerClient()
    d = Dispatcher(
        pool=pool, session_mgr=session_mgr, handler=handler,
        broker_client=broker_client, idle_timeout_sec=60,
    )

    await d.dispatch(_event("s", "topic-a", msg_id=1))
    await d.dispatch(_event("s", "topic-b", msg_id=2))

    # Both are processed before any idle timeout.
    ok = await _wait_for(lambda: len(handler.processed) == 2, timeout=2.0)
    assert ok, f"expected 2 processed, got {len(handler.processed)}"

    # The slot was released between A and B — in_use drops back to 0 right after.
    ok = await _wait_for(lambda: pool.in_use == 0, timeout=1.0)
    assert ok
    # The topic loops for A and B stay "alive" (sleeping on queue.get) but do not
    # hold a slot — exactly the point of the refactor.


async def test_two_concurrent_topics_with_pool_2(session_mgr: SessionManager):
    """With pool=2, two topics run the handler in parallel."""
    pool = WorkerPool(size=2)
    handler = StubHandler(delay_sec=0.1)
    d = Dispatcher(
        pool=pool, session_mgr=session_mgr, handler=handler,
        broker_client=StubBrokerClient(), idle_timeout_sec=60,
    )
    await d.dispatch(_event("s", "a"))
    await d.dispatch(_event("s", "b"))

    # Both must be in handle at the same time at some point.
    # Detected via pool.in_use reaching 2.
    ok = await _wait_for(lambda: pool.in_use == 2, timeout=1.0)
    assert ok, "expected 2 handlers running in parallel"


async def test_queue_ack_posted_when_pool_full(session_mgr: SessionManager):
    """If the pool is full at the start of a turn, posts the ⏳ Waiting for a slot ack."""
    pool = WorkerPool(size=1)
    slow = StubHandler(delay_sec=0.3)
    broker_client = StubBrokerClient()
    d = Dispatcher(
        pool=pool, session_mgr=session_mgr, handler=slow,
        broker_client=broker_client, idle_timeout_sec=60,
    )
    await d.dispatch(_event("s", "first"))
    # Make sure the first turn holds the slot
    await slow.handle_started.wait()
    # Second topic: pool full → must post an ack
    await d.dispatch(_event("s", "second"))
    ok = await _wait_for(
        lambda: any("Waiting for a slot" in c for (_, _, c) in broker_client.sent),
        timeout=1.0,
    )
    assert ok, f"expected a queue ack; received: {broker_client.sent}"


async def test_internal_topics_do_not_post_queue_ack(session_mgr: SessionManager):
    """`__ask-...` topics do not clutter the human's view with queue acks."""
    pool = WorkerPool(size=1)
    slow = StubHandler(delay_sec=0.3)
    broker_client = StubBrokerClient()
    d = Dispatcher(
        pool=pool, session_mgr=session_mgr, handler=slow,
        broker_client=broker_client, idle_timeout_sec=60,
    )
    await d.dispatch(_event("s", "first"))
    await slow.handle_started.wait()
    await d.dispatch(_event("s", "__ask-from-X-123"))
    # Let the loop run for a bit
    await asyncio.sleep(0.05)
    msgs = [c for (_, _, c) in broker_client.sent]
    assert not any("Waiting for a slot" in m for m in msgs), \
        f"internal topics must not post an ack: {msgs}"


# ---------------------------------------------------------------------------
# D-29: cancel_topic via control event.
# ---------------------------------------------------------------------------


async def test_cancel_without_task_returns_info(session_mgr: SessionManager):
    pool = WorkerPool(size=1)
    broker_client = StubBrokerClient()
    d = Dispatcher(
        pool=pool, session_mgr=session_mgr, handler=StubHandler(),
        broker_client=broker_client, idle_timeout_sec=60,
    )
    await d.dispatch({
        "_ctrl": True, "ctrl_type": "cancel_topic",
        "display_recipient": "s", "subject": "nothing",
    })
    msgs = [c for (_, _, c) in broker_client.sent]
    assert any("Nothing to cancel" in m for m in msgs), msgs


async def test_cancel_silent_posts_nothing(session_mgr: SessionManager):
    """A cancel triggered by archive/delete (silent=True) posts no confirmation
    msg in the conv — avoids cluttering the Closed tab."""
    pool = WorkerPool(size=1)
    broker_client = StubBrokerClient()
    d = Dispatcher(
        pool=pool, session_mgr=session_mgr, handler=StubHandler(),
        broker_client=broker_client, idle_timeout_sec=60,
    )
    await d.dispatch({
        "_ctrl": True, "ctrl_type": "cancel_topic",
        "display_recipient": "s", "subject": "nothing", "silent": True,
    })
    msgs = [c for (_, _, c) in broker_client.sent]
    assert not any("Nothing to cancel" in m for m in msgs), msgs
    assert not any("Cancelled by user" in m for m in msgs), msgs


async def test_cancel_before_running_drains_and_notifies(session_mgr: SessionManager):
    """A topic waiting on pool.acquire (held by another) can be cancelled."""
    pool = WorkerPool(size=1)
    slow = StubHandler(delay_sec=1.0)  # holds the slot
    broker_client = StubBrokerClient()
    d = Dispatcher(
        pool=pool, session_mgr=session_mgr, handler=slow,
        broker_client=broker_client, idle_timeout_sec=60,
    )
    await d.dispatch(_event("s", "first"))
    await slow.handle_started.wait()
    # Second topic comes in — it will wait on pool.acquire
    await d.dispatch(_event("s", "second"))
    # Wait for the queue ack to confirm second has entered _run_turn
    await _wait_for(
        lambda: any("Waiting for a slot" in c for (_, _, c) in broker_client.sent),
        timeout=1.0,
    )
    # Cancel
    await d.dispatch({
        "_ctrl": True, "ctrl_type": "cancel_topic",
        "display_recipient": "s", "subject": "second",
    })
    ok = await _wait_for(
        lambda: any("Cancelled by user" in c for (_, _, c) in broker_client.sent),
        timeout=1.0,
    )
    assert ok, f"expected a cancel confirmation; msgs: {broker_client.sent}"


async def test_cancel_during_run_without_registered_proc_returns_too_late(session_mgr: SessionManager):
    """If the handler is running but has not registered a proc (pre-D-71 or pre-spawn),
    cancel returns `too_late`."""
    pool = WorkerPool(size=1)
    # The handler "sticks" until we release it explicitly — more reliable than sleep.
    release = asyncio.Event()

    class StickyHandler(StubHandler):
        async def handle(self, event, topic_key, workdir):
            self.handle_started.set()
            await release.wait()
            self.processed.append((topic_key, event))

    handler = StickyHandler()
    broker_client = StubBrokerClient()
    d = Dispatcher(
        pool=pool, session_mgr=session_mgr, handler=handler,
        broker_client=broker_client, idle_timeout_sec=60,
    )
    await d.dispatch(_event("s", "t"))
    await handler.handle_started.wait()
    await d.dispatch({
        "_ctrl": True, "ctrl_type": "cancel_topic",
        "display_recipient": "s", "subject": "t",
    })
    ok = await _wait_for(
        lambda: any("Too late" in c for (_, _, c) in broker_client.sent),
        timeout=1.0,
    )
    assert ok, f"expected too_late; msgs: {broker_client.sent}"
    # Release the handler for a clean cleanup
    release.set()


# ---------------------------------------------------------------------------
# D-71: cancel with a registered proc sends SIGTERM + SIGKILL fallback.
# ---------------------------------------------------------------------------


class _FakeProc:
    """Minimal subset of asyncio.subprocess.Process to test the kill path.

    Captures terminate/kill + lets wait() block until manually released.
    """
    def __init__(self, ignore_terminate: bool = False):
        self.pid = 12345
        self.terminate_called = False
        self.kill_called = False
        self._exit_event = asyncio.Event()
        self._ignore_terminate = ignore_terminate

    def terminate(self):
        self.terminate_called = True
        if not self._ignore_terminate:
            self._exit_event.set()

    def kill(self):
        self.kill_called = True
        self._exit_event.set()

    async def wait(self) -> int:
        await self._exit_event.wait()
        return 0


async def test_cancel_during_run_with_registered_proc_sends_sigterm(session_mgr: SessionManager):
    """D-71: the handler registered a proc → cancel sends SIGTERM via Process.terminate()."""
    pool = WorkerPool(size=1)
    release = asyncio.Event()
    fake_proc = _FakeProc()

    class HandlerWithProc(StubHandler):
        def __init__(self, disp):
            super().__init__()
            self._disp = disp

        async def handle(self, event, topic_key, workdir):
            self.handle_started.set()
            self._disp.handler_register_proc(topic_key, fake_proc)
            try:
                await release.wait()
            finally:
                self._disp.handler_unregister_proc(topic_key)
            self.processed.append((topic_key, event))

    broker_client = StubBrokerClient()
    d = Dispatcher(
        pool=pool, session_mgr=session_mgr, handler=None,  # set later
        broker_client=broker_client, idle_timeout_sec=60,
    )
    d.handler = HandlerWithProc(d)
    await d.dispatch(_event("s", "t"))
    await d.handler.handle_started.wait()
    # Cancel: the dispatcher must find the registered proc and call terminate().
    await d.dispatch({
        "_ctrl": True, "ctrl_type": "cancel_topic",
        "display_recipient": "s", "subject": "t",
    })
    ok = await _wait_for(lambda: fake_proc.terminate_called, timeout=1.0)
    assert ok, "expected fake_proc.terminate() to be called"
    # The confirmation message mentions SIGTERM so the human understands.
    ok = await _wait_for(
        lambda: any("SIGTERM" in c for (_, _, c) in broker_client.sent),
        timeout=1.0,
    )
    assert ok, f"expected a confirmation mentioning SIGTERM; msgs: {broker_client.sent}"
    # Release for cleanup.
    release.set()


async def test_cancel_sigkill_fallback_if_sigterm_ignored(session_mgr: SessionManager):
    """D-71: if SIGTERM does not bring it down within 3s, SIGKILL kicks in."""
    pool = WorkerPool(size=1)
    release = asyncio.Event()
    fake_proc = _FakeProc(ignore_terminate=True)

    class HandlerWithProc(StubHandler):
        def __init__(self, disp):
            super().__init__()
            self._disp = disp

        async def handle(self, event, topic_key, workdir):
            self.handle_started.set()
            self._disp.handler_register_proc(topic_key, fake_proc)
            try:
                await release.wait()
            finally:
                self._disp.handler_unregister_proc(topic_key)
            self.processed.append((topic_key, event))

    broker_client = StubBrokerClient()
    d = Dispatcher(
        pool=pool, session_mgr=session_mgr, handler=None,
        broker_client=broker_client, idle_timeout_sec=60,
    )
    d.handler = HandlerWithProc(d)
    await d.dispatch(_event("s", "t"))
    await d.handler.handle_started.wait()
    await d.dispatch({
        "_ctrl": True, "ctrl_type": "cancel_topic",
        "display_recipient": "s", "subject": "t",
    })
    # Trigger the fallback with a short timeout (avoids waiting a real 3s in the test).
    asyncio.create_task(d._sigkill_fallback(
        TopicKey(stream="s", topic="t"), fake_proc, timeout_sec=0.1,
    ))
    ok = await _wait_for(lambda: fake_proc.kill_called, timeout=1.0)
    assert ok, "expected fake_proc.kill() to be called after the SIGTERM timeout"
    release.set()


async def test_handler_register_unregister_isolates_topics(session_mgr: SessionManager):
    """The _topic_procs dict is keyed by slug — different topics do not collide."""
    d = Dispatcher(
        pool=WorkerPool(size=1), session_mgr=session_mgr,
        handler=StubHandler(), idle_timeout_sec=60,
    )
    k1 = TopicKey(stream="s", topic="a")
    k2 = TopicKey(stream="s", topic="b")
    p1 = _FakeProc()
    p2 = _FakeProc()
    d.handler_register_proc(k1, p1)
    d.handler_register_proc(k2, p2)
    assert d._topic_procs[k1.slug()] is p1
    assert d._topic_procs[k2.slug()] is p2
    d.handler_unregister_proc(k1)
    assert k1.slug() not in d._topic_procs
    assert d._topic_procs[k2.slug()] is p2
    # Idempotent — unregistering a missing slug does not blow up.
    d.handler_unregister_proc(k1)
