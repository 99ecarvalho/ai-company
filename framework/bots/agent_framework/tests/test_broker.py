"""Tests do McpBroker — resolve, persistencia pending_questions."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent_framework.mcp.broker import McpBroker
from agent_framework.internal_client import TopicKey


@pytest.fixture
def broker(tmp_path: Path):
    return McpBroker(pending_dir=tmp_path / "pending")


async def test_register_and_lookup(broker: McpBroker):
    key = TopicKey(stream="debug", topic="t1")
    slug = broker.register_topic(key)
    assert broker.topic_for_slug(slug) == key
    assert broker.topic_for_slug("nao-existe") is None


async def test_ask_human_blocking_resolves(broker: McpBroker):
    key = TopicKey(stream="debug", topic="t1")
    broker.register_topic(key)

    received = []

    async def on_ask(k, q, ctx, blocking):
        received.append((q, ctx, blocking))

    broker.set_on_ask(on_ask)

    async def resolver():
        # da um pouco de tempo pra ask_human registrar o future
        for _ in range(50):
            if broker.has_pending(key):
                broker.resolve(key, "minha resposta")
                return
            await asyncio.sleep(0.01)
        pytest.fail("broker nunca teve pending")

    resolver_task = asyncio.create_task(resolver())
    result = await broker.ask_human(key, "qual cor?", context="teste", blocking=True)
    await resolver_task
    assert result == "minha resposta"
    assert received == [("qual cor?", "teste", True)]


async def test_ask_human_non_blocking_returns_fallback(broker: McpBroker):
    key = TopicKey(stream="debug", topic="t1")
    broker.register_topic(key)
    broker.set_on_ask(_noop)
    result = await broker.ask_human(
        key, "pergunta", blocking=False, fallback="segue com hipotese X"
    )
    assert result == "segue com hipotese X"


async def test_ask_human_persists_then_cleans(broker: McpBroker):
    key = TopicKey(stream="debug", topic="t1")
    broker.register_topic(key)
    broker.set_on_ask(_noop)

    async def observe_and_resolve():
        # espera o arquivo aparecer
        path = broker.pending_question_path(key)
        for _ in range(50):
            if path.exists():
                data = json.loads(path.read_text())
                assert data["question"] == "Q"
                broker.resolve(key, "resp")
                return
            await asyncio.sleep(0.01)
        pytest.fail("arquivo nunca apareceu")

    t = asyncio.create_task(observe_and_resolve())
    await broker.ask_human(key, "Q", blocking=True)
    await t
    # Depois limpa
    assert not broker.pending_question_path(key).exists()


async def test_resolve_no_pending_returns_false(broker: McpBroker):
    key = TopicKey(stream="x", topic="y")
    assert broker.resolve(key, "ignored") is False


async def test_ask_human_timeout_returns_fallback(broker: McpBroker):
    key = TopicKey(stream="debug", topic="slow")
    broker.register_topic(key)
    broker.set_on_ask(_noop)
    result = await broker.ask_human(
        key, "Q", blocking=True,
        timeout_minutes=0.005,  # 0.3s
        fallback="fallback-default",
    )
    assert result == "fallback-default"


async def _noop(*args, **kwargs):
    pass
