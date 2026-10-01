"""McpBroker tests — resolve, pending_questions persistence."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ai_company.mcp.broker import McpBroker
from ai_company.internal_client import TopicKey


@pytest.fixture
def broker(tmp_path: Path):
    return McpBroker(pending_dir=tmp_path / "pending")


async def test_register_and_lookup(broker: McpBroker):
    key = TopicKey(stream="debug", topic="t1")
    slug = broker.register_topic(key)
    assert broker.topic_for_slug(slug) == key
    assert broker.topic_for_slug("does-not-exist") is None


async def test_ask_human_resolves(broker: McpBroker):
    key = TopicKey(stream="debug", topic="t1")
    broker.register_topic(key)

    received = []

    async def on_ask(k, q, ctx, blocking):
        received.append((q, ctx, blocking))

    broker.set_on_ask(on_ask)

    async def resolver():
        # give ask_human a moment to register the future
        for _ in range(50):
            if broker.has_pending(key):
                broker.resolve(key, "my answer")
                return
            await asyncio.sleep(0.01)
        pytest.fail("broker never had a pending ask")

    resolver_task = asyncio.create_task(resolver())
    result = await broker.ask_human(key, "which color?", context="test")
    await resolver_task
    assert result == "my answer"
    assert received == [("which color?", "test", True)]


async def test_ask_human_persists_then_cleans(broker: McpBroker):
    key = TopicKey(stream="debug", topic="t1")
    broker.register_topic(key)
    broker.set_on_ask(_noop)

    async def observe_and_resolve():
        # wait for the file to appear
        path = broker.pending_question_path(key)
        for _ in range(50):
            if path.exists():
                data = json.loads(path.read_text())
                assert data["question"] == "Q"
                broker.resolve(key, "resp")
                return
            await asyncio.sleep(0.01)
        pytest.fail("file never appeared")

    t = asyncio.create_task(observe_and_resolve())
    await broker.ask_human(key, "Q")
    await t
    # Then it is cleaned up
    assert not broker.pending_question_path(key).exists()


async def test_resolve_no_pending_returns_false(broker: McpBroker):
    key = TopicKey(stream="x", topic="y")
    assert broker.resolve(key, "ignored") is False


async def _noop(*args, **kwargs):
    pass
