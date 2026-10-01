"""Tests for reconcile.py's agents.yaml handling (no broker or Docker needed).

Run: ./run.sh test unit   (or: cd framework/scripts && python -m pytest -q tests)
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("reconcile", ROOT / "framework/scripts/reconcile.py")
reconcile = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reconcile)

P = reconcile.MCP_PREFIX
GROUPS = reconcile.BUILTIN_TOOL_GROUPS


def _doc(agents: list[dict], **top) -> dict:
    base = {"name": "a", "display_name": "A", "description": "d"}
    return {"schema_version": 1, **top, "agents": [{**base, **a} for a in agents]}


def _tools(doc: dict) -> list[list[str]]:
    agents, _, _ = reconcile.validate_schema(doc)
    return [a["allowed_tools"] for a in agents]


# ---------- tool groups ----------

def test_group_expands_in_place_and_dedupes():
    out = reconcile.expand_tools(["Bash", "group:files", "Read", "group:memory"], GROUPS, "t")
    assert out == ["Bash", *GROUPS["files"], *GROUPS["memory"]]


def test_unknown_group_dies():
    with pytest.raises(SystemExit):
        reconcile.expand_tools(["group:nope"], GROUPS, "t")


def test_instance_groups_nest_and_override_builtins():
    groups = reconcile._validate_tool_groups({
        "reporting": ["group:web", "mcp__sentry__list_issues"],
        "files": ["Read"],  # overrides the built-in
    })
    assert reconcile.expand_tools(["group:reporting", "group:files"], groups, "t") == [
        "WebFetch", "WebSearch", "mcp__sentry__list_issues", "Read",
    ]


def test_group_cycle_dies():
    groups = reconcile._validate_tool_groups({"x": ["group:y"], "y": ["group:x"]})
    with pytest.raises(SystemExit):
        reconcile.expand_tools(["group:x"], groups, "t")


def test_defaults_are_prepended_unless_agent_opts_out():
    doc = _doc(
        [{"name": "a", "allowed_tools": ["Bash"]},
         {"name": "b", "streams": ["b"], "allowed_tools": ["Bash"], "inherit_tool_defaults": False}],
        allowed_tools_defaults=["group:human"],
    )
    a, b = _tools(doc)
    assert a == [*GROUPS["human"], "Bash"]
    assert b == ["Bash"]


def test_plain_tool_lists_still_work():
    assert _tools(_doc([{"allowed_tools": ["Read", P + "ask_human"]}])) == [["Read", P + "ask_human"]]


def test_builtin_groups_only_name_real_mcp_tools():
    tools_py = (ROOT / "framework/bots/ai_company/ai_company/mcp/tools.py").read_text()
    for group in GROUPS.values():
        for tool in group:
            if tool.startswith(P):
                assert f'"name": "{tool[len(P):]}"' in tools_py, tool


# ---------- worktree mount ----------

@pytest.mark.parametrize("agent, mode", [
    ({"write_access": ["repos"]}, ""),
    ({"write_access": ["company"], "read_access": ["repos"]}, ":ro"),
    ({"write_access": ["company"], "worktree_access": "rw"}, ""),
    ({"write_access": ["repos"], "worktree_access": "ro"}, ":ro"),
])
def test_worktree_mode(agent, mode):
    assert reconcile._worktree_mode(agent) == mode


def test_worktree_mount_in_service():
    svc = reconcile.build_agent_service({"name": "a", "write_access": ["company"]}, {})
    assert "${WORKTREES_DIR:-./instance/worktrees}:/workspace/worktrees:ro" in svc["volumes"]


def test_invalid_worktree_access_dies():
    with pytest.raises(SystemExit):
        _tools(_doc([{"worktree_access": "yes"}]))


# ---------- model ----------

def test_full_model_id_warns_and_alias_does_not(capsys):
    _tools(_doc([{"model": "opus"}]))
    assert "pins a specific release" not in capsys.readouterr().out
    _tools(_doc([{"model": "claude-opus-4-6"}]))
    assert "pins a specific release" in capsys.readouterr().out


# ---------- CLAUDE.md drift ----------

def test_drift_detects_framework_mechanics():
    assert reconcile.claude_md_drift(f"You may use: Read, {P}ask_human.")
    assert reconcile.claude_md_drift("Call complete_phase when done.")
    assert reconcile.claude_md_drift("Pass the baseline_sha from the plan.")


def test_role_text_is_not_drift():
    assert reconcile.claude_md_drift("Ask the human before changing the public API.") == []


def test_seed_claude_md_has_no_drift():
    seed = reconcile.DEFAULT_CLAUDE_MD.format(display_name="X", description="y")
    assert reconcile.claude_md_drift(seed) == []


# ---------- shipped example ----------

def test_example_agents_yaml_resolves():
    doc = yaml.safe_load((ROOT / "framework/examples/agents.yaml.example").read_text())
    agents, _, _ = reconcile.validate_schema(doc)
    by_name = {a["name"]: a for a in agents}
    for a in agents:
        assert P + "ask_human" in a["allowed_tools"], a["name"]
        assert P + "complete_phase" in a["allowed_tools"], a["name"]
        assert not any(t.startswith("group:") for t in a["allowed_tools"])
    assert {"Bash", P + "create_worktree"} <= set(by_name["executor"]["allowed_tools"])
    assert P + "create_worktree" not in by_name["reviewer"]["allowed_tools"]
    assert "WebSearch" in by_name["researcher"]["allowed_tools"]
    assert reconcile._worktree_mode(by_name["executor"]) == ""
    assert reconcile._worktree_mode(by_name["reviewer"]) == ""      # opts in to run tests
    assert reconcile._worktree_mode(by_name["triager"]) == ":ro"
