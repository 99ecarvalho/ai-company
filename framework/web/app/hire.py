"""Hire Assistant — generates CLAUDE.md + an agents.yaml entry for a new agent
by calling the Claude CLI via `docker exec` in an existing container (reuses
the Claude auth already mounted in agent-debug).
"""
from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import docker
import structlog
import yaml


log = structlog.get_logger("hire")

import os

# Reuses Claude auth by running `claude -p` inside an existing agent container
# (which already has the Claude CLI + creds mounted). Agent slug — the container name
# is derived dynamically via agent_bootstrap._container_name() to cover
# instances with a custom COMPOSE_PROJECT_NAME. A full override is still
# available via HIRE_AGENT_CONTAINER for exotic setups.
HIRE_AGENT = os.environ.get("HIRE_AGENT", "executor")


def _hire_container_name() -> str:
    """Container name resolved at runtime (not at import time) — _compose_project()
    reads COMPOSE_PROJECT_NAME from the environment, which may change between tests."""
    explicit = os.environ.get("HIRE_AGENT_CONTAINER")
    if explicit:
        return explicit
    from . import agent_bootstrap as _ab
    return _ab._container_name(HIRE_AGENT)

AGENTS_YAML = Path("/workspace/agents/agents.yaml")
AGENTS_DIR = Path("/workspace/agents")
PROJECT_ROOT = Path("/workspace")                      # inside the web container
PROJECT_ROOT_HOST = os.environ.get("PROJECT_ROOT_HOST", "/workspace")  # path on the daemon (host)

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")

VALID_MOUNTS = {"company", "orchestrator", "repos"}


# Prompt for Claude to generate CLAUDE.md + yaml entry (as a JSON object — we serialize to YAML in the backend)
GENERATION_PROMPT = """\
You are being hired to help the user set up an "employee" (a Claude Code agent)
for their virtual company. Based on the answers below, generate a complete configuration.

=== NEW AGENT DATA ===

{data_block}

=== RESPONSE FORMAT ===

Your response MUST be a single valid JSON object, with no markdown fences, containing exactly:

{{
  "entry": {{
    "name": "<slug>",
    "display_name": "<Display Name>",
    "description": "<one line>",
    "streams": ["<slug>"],
    "pool_size": <int>,
    "idle_timeout_sec": <int>,
    "write_access": ["<mount>", ...],
    "read_access": ["<mount>", ...],
    "memory": true,
    "model": "<optional: sonnet|opus|haiku>",
    "effort": "<optional: low|medium|high|xhigh|max>",
    "allowed_tools": ["<tool>", ...]
  }},
  "claude_md": "<string with the full CLAUDE.md markdown>"
}}

=== RULES ===

entry:
- name: exactly the slug given in "name (slug)" in the data above.
- pool_size: 2 by default; 1 if the role requires serialization; 3-5 for throughput.
- idle_timeout_sec: 900 by default; 1800-3600 for long tasks (research, implementation).
- write_access / read_access: lists. Options: "company", "orchestrator", "repos". Do NOT repeat the same mount in both.
- memory: true (default).
- model: OMIT by default. Include it only if the role needs a specific capability
  ("opus" for heavy reasoning; "haiku" for simple/fast/cheap tasks). Always an
  alias, never a full "claude-..." id (that pins an old release).
- effort: OMIT by default. Use "high" for roles that need deep reasoning
  (planner, reviewer); "low" for mechanical/repetitive tasks (simple capture).
- allowed_tools: use tool groups, written as "group:<name>". ALWAYS include
  "group:human" (ask_human) and "group:memory".
  If it takes part in a multi-agent workflow: "group:workflow".
  If it needs to read/edit files: "group:files".
  If it needs to run commands: "Bash".
  If it needs to search the web: "group:web".
  If it writes code in repos (worktrees): "group:worktree".
  Consider "group:agents" if the role will likely need to consult another agent.

claude_md:
- English.
- First line: "# <display_name>".
- Include the sections:
  * ## Persona / policy (voice, role principles, vocabulary)
  * ## Expected response format (which directory it writes to, file structure)
  * ## Persistent memory — state what SPECIFICALLY this agent should remember (role-related format preferences, architectural decisions, recurring names)
  * ## Limits (does not do) — state explicitly what it does NOT do and who to route that to
- Be SPECIFIC to the role's domain. Do not generate generic content.
- Do NOT describe framework tools or mechanics (tool names, complete_phase,
  worktrees, memory tool usage): the platform rules already cover them, and
  this file is about the role only.

Return ONLY the JSON. No text outside it, no fences.
"""


def validate_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise ValueError(f"invalid name '{name}': use lowercase a-z0-9- (max 31 chars)")


def validate_mounts(write_access: list[str], read_access: list[str]) -> None:
    for m in write_access or []:
        if m not in VALID_MOUNTS:
            raise ValueError(f"invalid write_access: {m!r}. Use {VALID_MOUNTS}")
    for m in read_access or []:
        if m not in VALID_MOUNTS:
            raise ValueError(f"invalid read_access: {m!r}. Use {VALID_MOUNTS}")
    overlap = set(write_access or []) & set(read_access or [])
    if overlap:
        raise ValueError(f"same mount in both write and read: {overlap}")


def build_data_block(data: dict) -> str:
    """Formats the wizard data as a readable block for the prompt."""
    def _fmt_list(v):
        return ", ".join(v) if v else "(none)"

    lines = [
        f"name (slug): {data.get('name')}",
        f"display_name: {data.get('display_name')}",
        f"description (one line): {data.get('description')}",
        "",
        "responsibilities (what it does):",
        data.get("responsibilities", "").strip() or "(not specified)",
        "",
        "what it does NOT do:",
        data.get("non_responsibilities", "").strip() or "(not specified)",
        "",
        f"style / tone: {data.get('style', '').strip() or '(not specified)'}",
        "",
        f"takes part in a multi-agent workflow (uses complete_phase): {data.get('workflow_participant')}",
        f"needs to write to: {_fmt_list(data.get('write_access') or [])}",
        f"needs to read: {_fmt_list(data.get('read_access') or [])}",
        f"needs to run commands (Bash): {data.get('needs_bash')}",
        f"needs to search the web: {data.get('needs_web')}",
    ]
    return "\n".join(lines)


async def generate_draft(data: dict) -> dict:
    """Calls claude -p via docker exec in agent-debug. Returns dict {yaml_entry, claude_md}."""
    name = data.get("name", "")
    validate_name(name)
    validate_mounts(data.get("write_access") or [], data.get("read_access") or [])

    prompt = GENERATION_PROMPT.format(data_block=build_data_block(data))

    container_name = _hire_container_name()
    # Auto-bootstrap: ensures the hire "host" agent is running
    # (covers fresh setup + restarts). Same helper used by onboard.
    from . import agent_bootstrap as _ab
    try:
        await _ab.ensure_agent_running(HIRE_AGENT, timeout=45.0)
    except Exception as e:
        raise RuntimeError(
            f"could not start hire-host agent '{HIRE_AGENT}' "
            f"(container '{container_name}'): {e}"
        )

    client = docker.from_env()
    container = client.containers.get(container_name)

    # exec_run with stdin=False; claude -p gets the prompt via argv
    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    hire_model = os.environ.get("HIRE_MODEL", "")
    if hire_model:
        cmd += ["--model", hire_model]
    log.info("hire.generating", container=container_name, cmd_len=len(prompt))
    import asyncio
    def _run():
        # user="node" + environment with the right HOME so the claude CLI finds its credentials
        return container.exec_run(
            cmd,
            stdout=True,
            stderr=True,
            demux=True,
            user="node",
            environment={"HOME": "/home/node"},
        )
    rc_and_out = await asyncio.to_thread(_run)
    rc = rc_and_out.exit_code
    stdout, stderr = rc_and_out.output if rc_and_out.output else (b"", b"")
    stdout_s = (stdout or b"").decode("utf-8", errors="replace")
    stderr_s = (stderr or b"").decode("utf-8", errors="replace")
    if rc != 0:
        log.error("hire.exec_failed", rc=rc, stderr=stderr_s[:500])
        raise RuntimeError(f"claude failed (rc={rc}): {stderr_s[:500]}")

    # claude --output-format json returns an envelope; extract `result`, the model's text
    try:
        envelope = json.loads(stdout_s)
        result_text = envelope.get("result", "").strip()
    except Exception:
        result_text = stdout_s.strip()

    # Strip markdown fences if present, then parse the inner JSON
    if result_text.startswith("```"):
        result_text = re.sub(r"^```(?:json)?\s*", "", result_text)
        result_text = re.sub(r"\s*```$", "", result_text)

    try:
        parsed = json.loads(result_text)
    except Exception:
        log.error("hire.parse_failed", preview=result_text[:400])
        raise RuntimeError(
            "Claude returned invalid format — expected JSON. "
            "Preview: " + result_text[:300]
        )

    entry = parsed.get("entry")
    claude_md = parsed.get("claude_md", "").strip()
    if not isinstance(entry, dict) or not claude_md:
        raise RuntimeError("Response missing entry (object) or claude_md")

    # Force the canonical name (what the user typed, not what Claude made up)
    entry["name"] = name
    # If Claude forgot streams, default to [name]
    if not entry.get("streams"):
        entry["streams"] = [name]

    # Serialize entry to valid YAML. safe_dump emits `agents:\n- name:` (indent 0).
    # We want only the item, indented 2 spaces (to sit under the
    # `agents:` already in the file).
    yaml_text = yaml.safe_dump({"agents": [entry]}, sort_keys=False, allow_unicode=True, default_flow_style=False)
    body_lines = yaml_text.splitlines()[1:]  # remove "agents:"
    # Re-indent each line by +2 spaces
    yaml_entry = "\n".join(("  " + l) if l else "" for l in body_lines).rstrip()

    log.info("hire.draft_ok", name=name, yaml_len=len(yaml_entry), md_len=len(claude_md))
    return {
        "yaml_entry": yaml_entry,
        "claude_md": claude_md,
        "name": name,
        "warning": None,
    }


async def apply_hire(payload: dict, *, skip_reconcile: bool = False) -> dict:
    """Writes CLAUDE.md + appends the entry to agents.yaml + runs reconcile.

    skip_reconcile: used by onboard_apply to batch — avoids running
    reconcile N times in quick succession (creates transient race conditions
    in the broker when 5 agents are created in a row). The caller is
    responsible for calling run_reconcile() once at the end.
    """
    name = payload.get("name", "").strip()
    # Do NOT use .strip() on yaml_entry — it removes the 2 leading spaces that
    # are part of the list item indent. Only rstrip.
    yaml_entry = (payload.get("yaml_entry") or "").rstrip()
    claude_md = (payload.get("claude_md") or "").strip()
    validate_name(name)
    if not yaml_entry or not claude_md:
        raise ValueError("yaml_entry and claude_md are required")

    # Already in agents.yaml?
    if AGENTS_YAML.exists():
        existing = AGENTS_YAML.read_text(encoding="utf-8")
        if re.search(rf"^\s*-\s*name:\s*{re.escape(name)}\s*$", existing, re.MULTILINE):
            raise ValueError(f"agent '{name}' already exists in agents.yaml")

    # Normalize yaml_entry: parse + re-serialize with the right indent.
    # yaml_entry arrives as a list item, indented 2 spaces (to be
    # appended under `agents:`). To parse it as root YAML, dedent first.
    try:
        lines = yaml_entry.splitlines()
        nonempty = [l for l in lines if l.strip()]
        common_indent = min((len(l) - len(l.lstrip(" "))) for l in nonempty) if nonempty else 0
        dedented = "\n".join(l[common_indent:] if len(l) >= common_indent else l for l in lines)
        parsed = yaml.safe_load(dedented)
        if isinstance(parsed, list) and parsed:
            entry_dict = parsed[0]
        elif isinstance(parsed, dict):
            entry_dict = parsed
        else:
            raise ValueError("yaml_entry is neither a list nor dict after parsing")
    except Exception as e:
        raise ValueError(f"yaml_entry is not valid YAML: {e}")

    # Re-serialize with a controlled indent
    yaml_text = yaml.safe_dump({"agents": [entry_dict]}, sort_keys=False, allow_unicode=True, default_flow_style=False)
    body_lines = yaml_text.splitlines()[1:]
    yaml_entry_clean = "\n".join(("  " + l) if l else "" for l in body_lines).rstrip()

    # Create dir + CLAUDE.md
    agent_dir = AGENTS_DIR / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "knowledge").mkdir(exist_ok=True)
    (agent_dir / "pending_questions").mkdir(exist_ok=True)
    (agent_dir / "CLAUDE.md").write_text(claude_md, encoding="utf-8")

    # Append the normalized yaml_entry to agents.yaml
    with AGENTS_YAML.open("a", encoding="utf-8") as f:
        f.write("\n" + yaml_entry_clean + "\n")

    log.info("hire.files_written", name=name)

    if skip_reconcile:
        log.info("hire.apply_ok", name=name, reconcile="deferred")
        return {
            "ok": True,
            "name": name,
            "reconcile_log": "",
            "note": "files written; reconcile deferred to caller",
        }

    # Run reconcile via a local subprocess — runs inside this web container,
    # which already has pyyaml + requests + scripts/ + framework/ copied at build.
    # Replaces the old "spawn ephemeral python:3.11-slim + pip install" — no
    # network for pip on every apply, stderr surfaces naturally, consistent paths.
    try:
        reconcile_log = run_reconcile()
    except Exception as e:
        log.exception("hire.reconcile_failed", name=name)
        raise RuntimeError(f"CLAUDE.md + yaml created, but reconcile failed: {e}")

    log.info("hire.apply_ok", name=name)
    return {
        "ok": True,
        "name": name,
        "reconcile_log": reconcile_log[-2000:] if reconcile_log else "",
        "note": f"Agent registered. To start the container: docker compose up -d agent-{name}",
    }


# Aliases for main.py — routes expect hire.apply / hire.draft.
apply = apply_hire
draft = generate_draft


RECONCILE_SCRIPT = "/app/scripts/reconcile.py"


def run_reconcile(extra_args: list[str] | None = None) -> str:
    """Runs reconcile.py in-process (local subprocess). Returns stdout+stderr
    combined. Raises RuntimeError on rc != 0 with stderr in the message.

    Path overrides: the instance .env has AGENTS_DIR/COMPANY_DIR/REPOS_DIR
    pointing relative to manager/ on the host (e.g. ../agents = the instance root).
    Inside the web container those paths don't resolve — override them with the
    absolute paths of the existing mounts (/workspace/agents, etc).
    """
    import subprocess
    import sys

    args = list(extra_args or []) + ["--no-up"]
    env = {
        **os.environ,
        **_dot_env(),
        "PROJECT_ROOT": "/workspace",
        # Existing mounts in the web container — bypass the relative paths
        # in the instance .env (which are relative to manager/ on the host).
        "AGENTS_DIR": "/workspace/agents",
        "COMPANY_DIR": "/workspace/company",
        "REPOS_DIR": "/workspace/repos",
        "SESSIONS_DIR": "/workspace/sessions",
        # Reconcile only does mkdir(exist_ok=True) on these — host-side paths
        # stay as ${BACKUPS_DIR:-...} in the generated override.yml, resolved
        # by compose on the host. /tmp is writable and ephemeral — ok.
        "BACKUPS_DIR": "/tmp/reconcile/backups",
        "WORKTREES_DIR": "/tmp/reconcile/worktrees",
        "HOOKS_DIR": "/tmp/reconcile/hooks",
    }
    proc = subprocess.run(
        [sys.executable, RECONCILE_SCRIPT, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    combined = proc.stdout + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    if proc.returncode != 0:
        log.error("reconcile.failed", rc=proc.returncode, stderr=proc.stderr[:2000])
        raise RuntimeError(
            f"reconcile failed (rc={proc.returncode}): {(proc.stderr or proc.stdout)[-500:]}"
        )
    return combined


def _dot_env() -> dict:
    """Reads the workspace .env to inject into the reconcile environment."""
    env: dict[str, str] = {}
    p = Path("/workspace/.env")
    if not p.exists():
        return env
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env
