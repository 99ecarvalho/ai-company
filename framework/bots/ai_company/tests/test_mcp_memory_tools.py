"""Tests dos handlers MCP memory_edit e memory_delete.

Usa McpServer real + broker real (vazio) + fake MemoryStore in-memory. Nao precisa
de Postgres.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_company.mcp.broker import McpBroker
from ai_company.mcp.server import McpServer


class FakeMemory:
    def __init__(self) -> None:
        self._facts: dict[str, dict] = {}
        self.enabled = True

    async def save(self, *, key: str, value: str, tags: list[str] | None = None) -> dict:
        action = "updated" if key in self._facts else "created"
        self._facts[key] = {"key": key, "value": value, "tags": list(tags or [])}
        return {"action": action, "key": key, "value": value, "tags": list(tags or [])}

    async def edit(self, *, key: str, value=None, tags=None) -> dict:
        if value is None and tags is None:
            raise ValueError("value ou tags obrigatorio")
        if key not in self._facts:
            raise KeyError(key)
        if value is not None:
            self._facts[key]["value"] = value
        if tags is not None:
            self._facts[key]["tags"] = list(tags)
        return {"action": "updated", **self._facts[key]}

    async def delete(self, key: str) -> bool:
        return self._facts.pop(key, None) is not None

    async def count(self) -> int:
        return len(self._facts)

    async def recall(self, query: str, limit: int = 5) -> list[dict]:
        return list(self._facts.values())[:limit]

    async def list_recent(self, limit: int = 20, tag=None) -> list[dict]:
        return list(self._facts.values())[:limit]


@pytest.fixture
def server(tmp_path: Path):
    broker = McpBroker(pending_dir=tmp_path / "pending")
    memory = FakeMemory()
    return McpServer(broker=broker, agent_name="tester", memory=memory), memory


async def _call(server: McpServer, name: str, arguments: dict):
    return await server._call_tool("s", 1, {"name": name, "arguments": arguments})


async def test_memory_edit_updates_existing(server):
    srv, mem = server
    await mem.save(key="k1", value="v1")
    resp = await _call(srv, "memory_edit", {"key": "k1", "value": "v2"})
    assert "error" not in resp
    assert "updated: k1" in resp["result"]["content"][0]["text"]
    assert mem._facts["k1"]["value"] == "v2"


async def test_memory_edit_preserves_tags_when_only_value_given(server):
    srv, mem = server
    await mem.save(key="k1", value="v1", tags=["a"])
    await _call(srv, "memory_edit", {"key": "k1", "value": "v2"})
    assert mem._facts["k1"]["tags"] == ["a"]


async def test_memory_edit_replaces_tags_when_given(server):
    srv, mem = server
    await mem.save(key="k1", value="v1", tags=["a"])
    await _call(srv, "memory_edit", {"key": "k1", "tags": ["b", "c"]})
    assert mem._facts["k1"]["tags"] == ["b", "c"]
    assert mem._facts["k1"]["value"] == "v1"


async def test_memory_edit_fails_on_missing_key(server):
    srv, _ = server
    resp = await _call(srv, "memory_edit", {"key": "missing", "value": "x"})
    assert "error" in resp
    assert "does not exist" in resp["error"]["message"]


async def test_memory_edit_requires_key(server):
    srv, _ = server
    resp = await _call(srv, "memory_edit", {"value": "x"})
    assert "error" in resp


async def test_memory_edit_requires_value_or_tags(server):
    srv, mem = server
    await mem.save(key="k1", value="v1")
    resp = await _call(srv, "memory_edit", {"key": "k1"})
    assert "error" in resp
    assert "value" in resp["error"]["message"]


async def test_memory_delete_removes_existing(server):
    srv, mem = server
    await mem.save(key="k1", value="v1")
    resp = await _call(srv, "memory_delete", {"key": "k1"})
    assert "error" not in resp
    assert "deleted: k1" in resp["result"]["content"][0]["text"]
    assert "k1" not in mem._facts


async def test_memory_delete_fails_on_missing_key(server):
    srv, _ = server
    resp = await _call(srv, "memory_delete", {"key": "ghost"})
    assert "error" in resp
    assert "does not exist" in resp["error"]["message"]


async def test_memory_delete_requires_key(server):
    srv, _ = server
    resp = await _call(srv, "memory_delete", {})
    assert "error" in resp
