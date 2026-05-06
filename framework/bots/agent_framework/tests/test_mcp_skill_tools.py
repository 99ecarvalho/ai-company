"""Tests dos handlers MCP save_skill / list_skills / delete_skill.

Skills moram em /app/agents/<name>/skills/<slug>/SKILL.md no container; nos
testes monkey-patchamos `_skills_root` pra apontar pra um tmp_path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_framework.mcp.broker import McpBroker
from agent_framework.mcp.server import McpServer


@pytest.fixture
def server(tmp_path: Path):
    broker = McpBroker(pending_dir=tmp_path / "pending")
    skills_root = tmp_path / "skills"
    srv = McpServer(broker=broker, agent_name="tester")
    # Override do path canonico (no container seria /app/agents/tester/skills).
    srv._skills_root = lambda: skills_root  # type: ignore[method-assign]
    return srv, skills_root


async def _call(srv: McpServer, name: str, arguments: dict):
    return await srv._call_tool("s", 1, {"name": name, "arguments": arguments})


# ---------- save_skill ----------

async def test_save_skill_creates_with_frontmatter(server):
    srv, root = server
    resp = await _call(srv, "save_skill", {
        "name": "lint-php",
        "description": "Lint a PHP file via phpcs",
        "body": "Run `phpcs path/to/file.php` and report violations.",
    })
    assert "error" not in resp, resp
    assert "created: lint-php" in resp["result"]["content"][0]["text"]
    skill_md = root / "lint-php" / "SKILL.md"
    assert skill_md.is_file()
    content = skill_md.read_text(encoding="utf-8")
    assert content.startswith("---\nname: lint-php\ndescription: Lint a PHP file via phpcs\n---\n")
    assert "Run `phpcs" in content


async def test_save_skill_upsert_returns_updated(server):
    srv, root = server
    await _call(srv, "save_skill", {"name": "s1", "description": "d", "body": "b1"})
    resp = await _call(srv, "save_skill", {"name": "s1", "description": "d2", "body": "b2"})
    assert "updated: s1" in resp["result"]["content"][0]["text"]
    content = (root / "s1" / "SKILL.md").read_text(encoding="utf-8")
    assert "description: d2" in content
    assert content.rstrip().endswith("b2")


@pytest.mark.parametrize("bad_name", [
    "Foo",            # uppercase
    "with space",     # space
    "../escape",      # traversal
    "a/b",            # slash
    "-leading-dash",  # leading hyphen
    "",               # empty
    "x" * 51,         # too long
])
async def test_save_skill_rejects_invalid_name(server, bad_name):
    srv, _ = server
    resp = await _call(srv, "save_skill", {"name": bad_name, "description": "d", "body": "b"})
    assert "error" in resp, resp
    assert "skill name" in resp["error"]["message"]


async def test_save_skill_rejects_oversize_body(server):
    srv, _ = server
    body = "x" * (100 * 1024 + 1)
    resp = await _call(srv, "save_skill", {"name": "big", "description": "d", "body": body})
    assert "error" in resp
    assert "body exceeds" in resp["error"]["message"]


async def test_save_skill_rejects_multiline_description(server):
    srv, _ = server
    resp = await _call(srv, "save_skill", {"name": "ok", "description": "line1\nline2", "body": "b"})
    assert "error" in resp
    assert "single line" in resp["error"]["message"]


async def test_save_skill_rejects_oversize_description(server):
    srv, _ = server
    resp = await _call(srv, "save_skill", {"name": "ok", "description": "x" * 501, "body": "b"})
    assert "error" in resp
    assert "description exceeds" in resp["error"]["message"]


async def test_save_skill_rejects_empty_body(server):
    srv, _ = server
    resp = await _call(srv, "save_skill", {"name": "ok", "description": "d", "body": "   "})
    assert "error" in resp
    assert "body is required" in resp["error"]["message"]


# ---------- list_skills ----------

async def test_list_skills_empty(server):
    srv, _ = server
    resp = await _call(srv, "list_skills", {})
    assert "no skills saved" in resp["result"]["content"][0]["text"]


async def test_list_skills_returns_name_and_description(server):
    srv, _ = server
    await _call(srv, "save_skill", {"name": "alpha", "description": "first", "body": "b"})
    await _call(srv, "save_skill", {"name": "beta", "description": "second", "body": "b"})
    resp = await _call(srv, "list_skills", {})
    text = resp["result"]["content"][0]["text"]
    assert "2 skill(s)" in text
    assert "- alpha: first" in text
    assert "- beta: second" in text


async def test_list_skills_skips_dirs_without_skill_md(server):
    srv, root = server
    # Cria diretorio sem SKILL.md — list ignora.
    (root / "stray").mkdir(parents=True)
    await _call(srv, "save_skill", {"name": "real", "description": "r", "body": "b"})
    resp = await _call(srv, "list_skills", {})
    text = resp["result"]["content"][0]["text"]
    assert "1 skill(s)" in text
    assert "stray" not in text


# ---------- delete_skill ----------

async def test_delete_skill_removes_directory(server):
    srv, root = server
    await _call(srv, "save_skill", {"name": "tmp", "description": "d", "body": "b"})
    assert (root / "tmp").exists()
    resp = await _call(srv, "delete_skill", {"name": "tmp"})
    assert "deleted: tmp" in resp["result"]["content"][0]["text"]
    assert not (root / "tmp").exists()


async def test_delete_skill_404(server):
    srv, _ = server
    resp = await _call(srv, "delete_skill", {"name": "nope"})
    assert "error" in resp
    assert "does not exist" in resp["error"]["message"]


async def test_delete_skill_validates_name(server):
    srv, _ = server
    resp = await _call(srv, "delete_skill", {"name": "../etc/passwd"})
    assert "error" in resp
    assert "skill name" in resp["error"]["message"]
