#!/usr/bin/env python3
"""Reconcile instance/agents/agents.yaml -> actual state.

Idempotent:
  1. Parse + schema validation
  2. For each agent:
     a. Create user (kind=bot) in the internal broker → get api_token → save to .env
     b. Create stream(s) + subscribe the bot
     c. Ensure instance/agents/<name>/ (CLAUDE.md from the template if missing; knowledge/, pending_questions/)
     d. Write the derived instance/agents/<name>/agent.yaml
  3. Generate docker-compose.override.yml (one service per agent)

Run:
  framework/scripts/reconcile.sh                # apply + docker compose up -d
  framework/scripts/reconcile.sh --dry-run
  framework/scripts/reconcile.sh --no-broker    # skip creating users/streams (FS + override only)
  framework/scripts/reconcile.sh --no-up
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
from pathlib import Path
from typing import Any

import requests
import yaml


# ---------- Constants ----------

PROJECT_ROOT = Path(os.environ.get("PROJECT_ROOT", ".")).resolve()
INSTANCE_DIR = PROJECT_ROOT / "instance"
ENV_FILE = PROJECT_ROOT / ".env"
TEMPLATE_DIR = PROJECT_ROOT / "framework" / "agent-template"
OVERRIDE_FILE = PROJECT_ROOT / "docker-compose.override.yml"


def _env_path(var: str, default_rel: str) -> Path:
    """Resolve a path from an env var (in .env or the environment), falling
    back to PROJECT_ROOT/<default_rel> if absent. Relative paths are anchored
    at PROJECT_ROOT (matches docker-compose semantics)."""
    val = os.environ.get(var, "")
    if not val and ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith(f"{var}=") and not s.startswith("#"):
                val = s.partition("=")[2].strip()
                break
    if not val:
        return (PROJECT_ROOT / default_rel).resolve()
    p = Path(val)
    return p.resolve() if p.is_absolute() else (PROJECT_ROOT / p).resolve()


AGENTS_DIR = _env_path("AGENTS_DIR", "instance/agents")
COMPANY_DIR = _env_path("COMPANY_DIR", "instance/company")
BACKUPS_DIR = _env_path("BACKUPS_DIR", "instance/backups")
SESSIONS_DIR = _env_path("SESSIONS_DIR", "instance/sessions")
WORKTREES_DIR = _env_path("WORKTREES_DIR", "instance/worktrees")
HOOKS_DIR = _env_path("HOOKS_DIR", "instance/hooks")
REPOS_DIR = _env_path("REPOS_DIR", "instance/repos")  # D-115: enumerate subdirs with .git/ for the split mount
AGENTS_YAML = AGENTS_DIR / "agents.yaml"

VALID_MOUNTS = {"company", "orchestrator", "repos"}
MCP_PREFIX = "mcp__ai_company__"
LEGACY_MCP_PREFIX = "mcp__agent_framework__"
AGENT_IMAGE = "ai-company/agent:0.1.0"

NAME_MAX_LEN = 31  # 1 leading + up to 30 more. Generous cap for DNS/stream.
NAME_RE = re.compile(rf"^[a-z][a-z0-9-]{{0,{NAME_MAX_LEN - 1}}}$")

# MCP capabilities come in two categories (D-119):
#
# (1) MCP_CAPABILITIES — singletons predefined in the framework. Used by
#     name directly in `agent.capabilities`. For capabilities that naturally
#     don't have multiple instances per installation (e.g. Sentry token per
#     org, single Playwright pool). Two shapes:
#       - HTTP sidecar: `service` (Compose service) + `server` pointing at the
#         sidecar URL. Reconcile injects the service into the override and
#         adds depends_on to the agents that declare the capability.
#       - In-process stdio: just `server` with type: stdio (no `service`).
#         Claude Code spawns it in the agent's container.
#     `agent_env` (optional) lists env vars injected into the agent container.
#
# (2) CAPABILITY_TEMPLATES — parameterizable templates. The instance declares
#     the name + env values in `agents.yaml` -> `capability_instances`.
#     Allows `mysql-production` + `mysql-staging` with different creds,
#     without leaking instance vocabulary into the framework. Stdio-only for
#     now (parameterizable HTTP sidecar = v2 when needed).
#
# Resolved via `resolve_capability(name, instances)` — instances take
# precedence over singletons on a name collision.

MCP_CAPABILITIES: dict[str, dict] = {
    "playwright": {
        "server": {"type": "http", "url": "http://playwright-mcp:8931/mcp"},
        "service": {
            "image": "ai-company/playwright-mcp:0.1.0",
            "build": {
                "context": ".",
                "dockerfile": "framework/docker/playwright-mcp.Dockerfile",
            },
            "restart": "unless-stopped",
        },
    },
}

CAPABILITY_TEMPLATES: dict[str, dict] = {
    "mysql": {
        # @benborla29/mcp-server-mysql: native stdio, reads creds from env.
        # Read-only by default (ALLOW_*_OPERATION unset).
        "server": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@benborla29/mcp-server-mysql"],
        },
        # Env vars the MCP server reads. The validator rejects instance keys
        # outside this list (catches typos). Instances use Compose-style
        # template strings (`${DB_FOO:-}`) that reconcile passes to the agent
        # container in build_agent_service; Compose interpolates at up time.
        "env_keys": ["MYSQL_HOST", "MYSQL_PORT", "MYSQL_USER", "MYSQL_PASS", "MYSQL_DB"],
    },
    "sentry": {
        # @sentry/mcp-server: native stdio. Runs in the agent's own container
        # via npx — no sidecar. Token scoped per Sentry org; empty host =
        # SaaS sentry.io, set = self-hosted.
        "server": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@sentry/mcp-server@latest"],
        },
        "env_keys": ["SENTRY_ACCESS_TOKEN", "SENTRY_HOST"],
    },
}


def known_capabilities(instances: dict) -> set[str]:
    return set(MCP_CAPABILITIES) | set(instances or {})


def resolve_capability(name: str, instances: dict) -> dict | None:
    """Return the unified capability spec, or None if unknown.

    Shape: {server, service|None, agent_env}. Instances take precedence
    over singletons on a name collision.
    """
    inst = (instances or {}).get(name)
    if inst is not None:
        tpl = CAPABILITY_TEMPLATES[inst["template"]]
        return {
            "server": dict(tpl["server"]),
            "service": None,
            "agent_env": dict(inst.get("env") or {}),
        }
    spec = MCP_CAPABILITIES.get(name)
    if spec is None:
        return None
    return {
        "server": dict(spec["server"]),
        "service": dict(spec["service"]) if "service" in spec else None,
        "agent_env": dict(spec.get("agent_env") or {}),
    }


# ---------- Util ----------

def log(msg: str, level: str = "info") -> None:
    prefix = {"info": "→", "ok": "✓", "warn": "!", "err": "✗", "step": "=="}.get(level, "·")
    print(f"{prefix} {msg}", flush=True)


def die(msg: str) -> None:
    log(msg, "err")
    sys.exit(1)


def load_env(path: Path) -> dict[str, str]:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def upsert_env(path: Path, kv: dict[str, str]) -> None:
    existing_lines: list[str] = []
    if path.exists():
        existing_lines = path.read_text(encoding="utf-8").splitlines()
    present: set[str] = set()
    out: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.partition("=")[0].strip()
            if k in kv:
                out.append(f"{k}={kv[k]}")
                present.add(k)
                continue
        out.append(line)
    new_keys = [k for k in kv if k not in present]
    if new_keys:
        if out and out[-1] != "":
            out.append("")
        out.append("# Agents (generated by reconcile)")
        for k in new_keys:
            out.append(f"{k}={kv[k]}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


# ---------- Schema validation ----------

def _validate_hooks_block(value: Any, ctx: str) -> None:
    """Validate the structure of a `hooks` block (defaults or per-agent).

    Passed through to Claude Code's settings.json — see
    https://code.claude.com/docs/en/hooks-guide. We validate the SHAPE
    (mapping of event->list of groups with `hooks: [...]`), not the semantic
    content (event names, hook types, matchers) — the CLI validates on load
    and the set of events keeps evolving.
    """
    if value is None:
        return
    if not isinstance(value, dict):
        die(f"{ctx}: `hooks` must be a mapping event->list")
    for event, groups in value.items():
        if not isinstance(event, str) or not event:
            die(f"{ctx}: `hooks` key must be an event name (non-empty string), got {event!r}")
        if not isinstance(groups, list):
            die(f"{ctx}.hooks.{event}: must be a list of groups")
        for j, g in enumerate(groups):
            gctx = f"{ctx}.hooks.{event}[{j}]"
            if not isinstance(g, dict):
                die(f"{gctx}: must be a mapping")
            inner = g.get("hooks")
            if not isinstance(inner, list) or not inner:
                die(f"{gctx}: `hooks` field (list of commands) is required and must be non-empty")
            for k, h in enumerate(inner):
                hctx = f"{gctx}.hooks[{k}]"
                if not isinstance(h, dict) or not isinstance(h.get("type"), str):
                    die(f"{hctx}: must have `type` (string)")


def merge_hooks(defaults: dict | None, per_agent: dict | None) -> dict:
    """Merge hooks_defaults + agent.hooks, concatenating per event.

    Semantics: the defaults' list of groups comes first, the agent's after.
    Each group is kept intact (Claude Code handles each group independently
    — matchers don't overlap). Events present on only one side appear as
    they are in the result.
    """
    out: dict[str, list] = {}
    for src in (defaults or {}, per_agent or {}):
        for event, groups in src.items():
            out.setdefault(event, []).extend(groups)
    return out


def _validate_capability_instances(value: Any) -> dict:
    """Validate the top-level `capability_instances` block (D-119).

    Shape:
        capability_instances:
          <instance-name>:
            template: <template-name in CAPABILITY_TEMPLATES>
            env: { KEY: VALUE-OR-COMPOSE-TEMPLATE-STRING, ... }

    instance-name becomes the MCP namespace (mcp__<name>__*) and also the
    server name in mcp.extra.json. env keys are validated against the template.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        die("`capability_instances` must be a mapping name->spec")
    out: dict[str, dict] = {}
    for name, spec in value.items():
        ctx = f"capability_instances.{name}"
        if not isinstance(name, str) or not NAME_RE.match(name):
            die(f"{ctx}: invalid name (must match /^[a-z][a-z0-9-]*$/)")
        if not isinstance(spec, dict):
            die(f"{ctx}: must be a mapping with `template` + `env`")
        tpl_name = spec.get("template")
        if not isinstance(tpl_name, str):
            die(f"{ctx}.template: required (string)")
        tpl = CAPABILITY_TEMPLATES.get(tpl_name)
        if tpl is None:
            die(
                f"{ctx}.template: unknown template {tpl_name!r}. "
                f"Known: {sorted(CAPABILITY_TEMPLATES)}"
            )
        env = spec.get("env") or {}
        if not isinstance(env, dict):
            die(f"{ctx}.env: must be a mapping KEY->value")
        valid_keys = set(tpl.get("env_keys") or [])
        for k, v in env.items():
            if not isinstance(k, str) or not k:
                die(f"{ctx}.env: keys must be non-empty strings")
            if valid_keys and k not in valid_keys:
                die(
                    f"{ctx}.env.{k}: key not recognized by template "
                    f"{tpl_name!r}. Accepted: {sorted(valid_keys)}"
                )
            if not isinstance(v, (str, int)):
                die(f"{ctx}.env.{k}: must be a string (supports ${{VAR:-default}})")
        # Collision with a singleton: the instance wins (resolve_capability already
        # does that), but warn to avoid surprises.
        if name in MCP_CAPABILITIES:
            log(
                f"{ctx}: instance overrides the same-named singleton in MCP_CAPABILITIES",
                "warn",
            )
        out[name] = {"template": tpl_name, "env": {k: str(v) for k, v in env.items()}}
    return out


def validate_schema(data: dict) -> tuple[list[dict], dict, dict]:
    if not isinstance(data, dict):
        die("agents.yaml must be a mapping at the top level")
    if data.get("schema_version") != 1:
        die("schema_version must be 1")
    hooks_defaults = data.get("hooks_defaults") or {}
    _validate_hooks_block(hooks_defaults, "hooks_defaults")
    capability_instances = _validate_capability_instances(data.get("capability_instances"))
    known_caps = known_capabilities(capability_instances)
    agents = data.get("agents") or []
    if not isinstance(agents, list):
        die("`agents` must be a list")
    seen_names: set[str] = set()
    seen_streams: set[str] = set()
    for i, a in enumerate(agents):
        ctx = f"agents[{i}]"
        if not isinstance(a, dict):
            die(f"{ctx}: must be a mapping")
        name = a.get("name")
        if not name:
            die(f"{ctx}: `name` is required")
        if not NAME_RE.match(name):
            if len(name) > NAME_MAX_LEN:
                die(f"{ctx}: invalid `name`: {name!r} has {len(name)} chars "
                    f"(max {NAME_MAX_LEN}). Regex: ^[a-z][a-z0-9-]{{0,{NAME_MAX_LEN - 1}}}$")
            die(f"{ctx}: invalid `name`: {name!r}. "
                f"Must start with [a-z] and contain only [a-z0-9-], up to {NAME_MAX_LEN} chars.")
        if name in seen_names:
            die(f"{ctx}: duplicate name: {name!r}")
        seen_names.add(name)
        if not a.get("display_name"):
            die(f"{ctx}: `display_name` is required")
        if not a.get("description"):
            die(f"{ctx}: `description` is required")
        streams = a.get("streams") or [name]
        if not isinstance(streams, list) or not streams:
            die(f"{ctx}: `streams` must be a non-empty list")
        for s in streams:
            if s in seen_streams:
                die(f"{ctx}: duplicate stream: {s!r}")
            seen_streams.add(s)
        for key in ("write_access", "read_access"):
            val = a.get(key) or []
            if not isinstance(val, list):
                die(f"{ctx}: `{key}` must be a list")
            for m in val:
                if m not in VALID_MOUNTS:
                    die(f"{ctx}: `{key}` unknown mount: {m!r}")
        overlap = set(a.get("write_access") or []) & set(a.get("read_access") or [])
        if overlap:
            die(f"{ctx}: overlap write/read: {overlap}")
        pool = a.get("pool_size", 2)
        if not isinstance(pool, int) or pool < 1:
            die(f"{ctx}: `pool_size` must be an int >= 1")
        idle = a.get("idle_timeout_sec", 900)
        if not isinstance(idle, int) or idle < 1:
            die(f"{ctx}: `idle_timeout_sec` must be an int >= 1")
        model = a.get("model")
        if model is not None and not isinstance(model, str):
            die(f"{ctx}: `model` must be a string")
        effort = a.get("effort")
        if effort is not None and effort not in ("low", "medium", "high", "xhigh", "max"):
            die(f"{ctx}: invalid `effort`: {effort!r}")
        caps = a.get("capabilities") or []
        if not isinstance(caps, list):
            die(f"{ctx}: `capabilities` must be a list")
        for c in caps:
            if c not in known_caps:
                die(
                    f"{ctx}: unknown capability: {c!r}. "
                    f"Known: {sorted(known_caps)}"
                )
        image = a.get("image")
        if image is not None and not isinstance(image, str):
            die(f"{ctx}: `image` must be a string (e.g. registry.gitlab.com/acme/my-agent:v1)")
        _validate_hooks_block(a.get("hooks"), ctx)
    return agents, hooks_defaults, capability_instances


# ---------- Broker admin client ----------

class BrokerAdmin:
    """HTTP client for the internal broker."""

    def __init__(self, url: str, admin_token: str):
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}

    def _get(self, path: str) -> Any:
        r = requests.get(f"{self.url}{path}", headers=self.headers, timeout=15)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, data: dict) -> dict:
        r = requests.post(f"{self.url}{path}", headers=self.headers, json=data, timeout=15)
        if r.status_code >= 400:
            raise RuntimeError(f"{path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def _delete(self, path: str) -> tuple[int, str]:
        r = requests.delete(f"{self.url}{path}", headers=self.headers, timeout=15)
        return r.status_code, r.text

    def _patch(self, path: str, data: dict) -> tuple[int, str]:
        r = requests.patch(f"{self.url}{path}", headers=self.headers, json=data, timeout=15)
        return r.status_code, r.text

    def list_users(self) -> list[dict]:
        return self._get("/api/users")

    def list_streams(self) -> list[dict]:
        resp = self._get("/api/streams")
        # Broker returns {streams: [...], default: ...} for the PWA
        return resp.get("streams", []) if isinstance(resp, dict) else resp

    def upsert_user(self, email: str, username: str, full_name: str, kind: str, agent_name: str | None) -> dict:
        return self._post("/api/users", {
            "email": email, "username": username, "full_name": full_name,
            "kind": kind, "agent_name": agent_name, "is_admin": False,
        })

    def upsert_stream(self, name: str, description: str | None = None) -> dict:
        return self._post("/api/streams", {"name": name, "description": description})

    def subscribe(self, user_id: int, stream: str) -> None:
        self._post("/api/subscriptions", {"user_id": user_id, "stream": stream})

    def delete_stream(self, name: str) -> tuple[int, str]:
        return self._delete(f"/api/streams/{name}")

    def delete_user(self, username: str) -> tuple[int, str]:
        return self._delete(f"/api/users/{username}")

    def set_stream_active(self, name: str, is_active: bool) -> tuple[int, str]:
        return self._patch(f"/api/streams/{name}/active", {"is_active": is_active})

    def set_user_active(self, username: str, is_active: bool) -> tuple[int, str]:
        return self._patch(f"/api/users/{username}/active", {"is_active": is_active})


# ---------- Template filesystem ----------

AGENT_YAML_TEMPLATE = """\
# Generated by reconcile from instance/agents/agents.yaml.
# Manual edits here are overwritten on the next reconcile.
name: {name}
description: {description}

streams:
{streams_block}
pool_size: {pool_size}

idle_timeout_sec: {idle_timeout_sec}

allowed_tools:
{tools_block}
memory:
  enabled: {mem_enabled}
  auto_inject_limit: {mem_auto_inject}
{model_block}{effort_block}{main_repo_block}"""

DEFAULT_CLAUDE_MD = """\
# {display_name}

{description}

## Persona / policy
Fill in the agent's voice, operating principles, vocabulary, and tone.

## Expected response format
Describe what this agent should produce (which directory, structure, etc).

## Available tools (auto-approved)
This agent can use without asking: {tools_list}.

If you need a tool that is NOT in this list, **don't try to call it
anyway** — it will dead-end at a permission prompt (we run non-interactive).
Instead: tell the human the tool isn't enabled and forward the request to an
agent with that capability, OR ask the human to adjust allowed_tools in
agents.yaml.

## Operational honesty (mandatory)
- If a tool failed, was blocked, or doesn't exist: **say it explicitly** to
  the human. Don't invent a successful result or describe outcomes you didn't
  produce.
- You do not "automatically detect" file changes made by other agents. If a
  file has `status: done` but you weren't the one who updated it,
  **don't claim authorship** — say "someone/another agent finished this" or,
  better yet, read the file and report what's there without assuming who did it.
- Clearly distinguish: what YOU just did vs. what you read from a file vs.
  what you inferred. When in doubt, read and quote; don't invent.

## Persistent memory (if enabled)
Before each run, the most relevant facts are injected into your context
(the "## Memory" block in the prompt). You can:
- memory_save(key, value, tags): record a fact (upsert by key — re-save overwrites)
- memory_recall(query): search facts
- memory_list(): list recent ones
- memory_edit(key, value?, tags?): update an existing fact (fails if key doesn't exist)
- memory_delete(key): remove a fact permanently (hard delete, no undo)

Save: user preferences, architectural decisions, conventions, recurring names.
Don't save: in-progress work, ephemeral state. When you discover a saved fact
is wrong or outdated, prefer memory_edit (fix) or memory_delete (remove) over
letting cruft pile up.

## Limits (does not do)
Describe what this agent does NOT do — forward to whom.
"""


def _memory_cfg(agent: dict) -> tuple[bool, int]:
    mem = agent.get("memory")
    if mem is None:
        return True, 5
    if isinstance(mem, bool):
        return mem, (5 if mem else 0)
    if isinstance(mem, dict):
        return bool(mem.get("enabled", True)), int(mem.get("auto_inject_limit", 5))
    return True, 5


def ensure_agent_dir(agent: dict, hooks_defaults: dict | None = None) -> None:
    import json as _json
    name = agent["name"]
    d = AGENTS_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "knowledge").mkdir(exist_ok=True)
    (d / "pending_questions").mkdir(exist_ok=True)
    # Sessions (runtime cwds per topic) live in ${SESSIONS_DIR}/<name>/ —
    # outside agent_home to separate ephemeral runtime from persistent config (D-51).
    (SESSIONS_DIR / name).mkdir(parents=True, exist_ok=True)

    claude_md = d / "CLAUDE.md"
    if not claude_md.exists():
        tools_list = ", ".join(agent.get("allowed_tools") or []) or "(none)"
        claude_md.write_text(
            DEFAULT_CLAUDE_MD.format(
                display_name=agent["display_name"],
                description=agent["description"],
                tools_list=tools_list,
            ),
            encoding="utf-8",
        )
        log(f"  CLAUDE.md created ({name})", "ok")

    # .claude/settings.json — mirrors allowed_tools into permissions.allow.
    # Claude Code (CLI, -p mode) reads it when the session starts. Without it,
    # our MCP tools hit a permission prompt that dies in non-interactive mode.
    claude_settings_dir = d / ".claude"
    claude_settings_dir.mkdir(exist_ok=True)
    allowed = list(agent.get("allowed_tools") or [])
    settings: dict = {"permissions": {"allow": allowed}}
    # Claude Code hooks: merge of hooks_defaults (top-level) + agent.hooks,
    # concatenated per event. Passed through to settings.json — the CLI
    # validates and runs them. Hook scripts must use absolute paths inside
    # the container (e.g. /app/agents/<name>/hooks/foo.sh). jq is available
    # in the agent image (framework/docker/agent.Dockerfile).
    merged_hooks = merge_hooks(hooks_defaults, agent.get("hooks"))
    if merged_hooks:
        settings["hooks"] = merged_hooks
    (claude_settings_dir / "settings.json").write_text(
        _json.dumps(settings, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    streams_block = "\n".join(f"  - {s}" for s in (agent.get("streams") or [name]))
    tools_block = "\n".join(f"  - {t}" for t in (agent.get("allowed_tools") or []))
    mem_enabled, mem_auto_inject = _memory_cfg(agent)
    model = agent.get("model")
    effort = agent.get("effort")
    thinking = agent.get("thinking")
    if thinking and not effort:
        effort = "high"
    model_block = f"\nmodel: {model}\n" if model else ""
    effort_block = f"effort: {effort}\n" if effort else ""
    main_repo = agent.get("main_repo")
    main_repo_block = f"\nmain_repo: {main_repo}\n" if main_repo else ""
    # description quoted with json.dumps: a JSON string is valid YAML and
    # escapes quotes/special characters correctly. Without it, descriptions
    # containing ":" (e.g. "Phase 1:...", "Advisory:...") break YAML parsing
    # in the bot (read as a mapping key).
    (d / "agent.yaml").write_text(
        AGENT_YAML_TEMPLATE.format(
            name=name, description=json.dumps(agent["description"], ensure_ascii=False),
            streams_block=streams_block, pool_size=agent.get("pool_size", 2),
            idle_timeout_sec=agent.get("idle_timeout_sec", 900), tools_block=tools_block,
            mem_enabled=str(mem_enabled).lower(), mem_auto_inject=mem_auto_inject,
            model_block=model_block, effort_block=effort_block,
            main_repo_block=main_repo_block,
        ),
        encoding="utf-8",
    )


# ---------- Docker compose override ----------

def build_agent_service(agent: dict, capability_instances: dict | None = None) -> dict:
    name = agent["name"]
    capability_instances = capability_instances or {}
    upper = name.upper().replace("-", "_")
    mem_enabled, _ = _memory_cfg(agent)
    # BYOI: an agent can point at its own image (public/private registry).
    # In that case we don't build — the user is responsible for the image.
    image_override = agent.get("image")
    svc: dict = {
        "image": image_override or AGENT_IMAGE,
        "restart": "unless-stopped",
        "depends_on": {"postgres": {"condition": "service_healthy"}, "web": {"condition": "service_started"}},
        "environment": {
            "AGENT_NAME": name,
            "BROKER_URL": "http://web:8090",
            "BROKER_TOKEN": f"${{AGENT_{upper}_TOKEN}}",
            "DATABASE_URL": "postgres://${POSTGRES_USER:-ai_company}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB:-ai_company}",
            "LOG_LEVEL": "INFO",
            "TRANSCRIBER_URL": "http://transcriber:8000",
            "TRANSCRIBER_LANGUAGE": "${TRANSCRIBER_LANGUAGE:-}",
            "TELEMETRY_URL": "http://web:8090/api/telemetry/event",
            "CLAUDE_MOCK": "${CLAUDE_MOCK:-}",
            # Azure AI Foundry (Claude via Azure) — if set, the CLI uses this
            # provider instead of Anthropic directly.
            "CLAUDE_CODE_USE_FOUNDRY": "${CLAUDE_CODE_USE_FOUNDRY:-}",
            "ANTHROPIC_FOUNDRY_RESOURCE": "${ANTHROPIC_FOUNDRY_RESOURCE:-}",
            "ANTHROPIC_FOUNDRY_API_KEY": "${ANTHROPIC_FOUNDRY_API_KEY:-}",
            "POOL_SIZE": "${POOL_SIZE:-}",
            "IDLE_TIMEOUT_SEC": "${IDLE_TIMEOUT_SEC:-}",
            "TZ": "${TZ:-America/Sao_Paulo}",
            # Git push / MR-PR — used by agents that push code.
            # Present in every agent's env; agents that don't push simply
            # never use them (CLAUDE.md defines the discipline).
            "GITLAB_TOKEN": "${GITLAB_TOKEN:-}",
            "GITLAB_HOST": "${GITLAB_HOST:-}",
            "GH_TOKEN": "${GH_TOKEN:-}",
            "GIT_AUTHOR_NAME": "${GIT_AUTHOR_NAME:-ai-company}",
            "GIT_AUTHOR_EMAIL": "${GIT_AUTHOR_EMAIL:-agents@local}",
            # glab reads GITLAB_TOKEN by default, but also accepts GLAB_TOKEN.
            # gh accepts GH_TOKEN directly. Nothing else to configure at runtime.
        },
        "volumes": [
            # D-90: dir bind of the host's whole ~/.claude/ (read-only) at a
            # staging path. Directories resolve by path in the kernel — when
            # the host's Claude CLI refreshes .credentials.json (write-then-rename
            # = new inode), the container sees the new version on the next read.
            # Pre-spawn sync in claude_runner.py copies it to the agent volume.
            # The previous file bind froze on the old inode and caused the
            # silent 401 described in D-55.
            "${HOME}/.claude/:/tmp/host-claude/:ro",
            # .claude.json stays a file bind (less critical, rarely rotated).
            # If it becomes a problem, same fix: mount ~/ or symlink on the host.
            "${HOME}/.claude.json:/tmp/claude-auth/.claude.json:ro",
            f"agent-{name}-claude:/home/node/.claude",
            f"${{AGENTS_DIR:-./instance/agents}}/{name}:/app/agents/{name}",
            # D-51: sessions (runtime cwds per topic) outside AGENTS_DIR.
            # Separates the agent's persistent config (agent.yaml, CLAUDE.md,
            # knowledge/) from ephemeral state (session_id, symlinks, 24h GC).
            f"${{SESSIONS_DIR:-./instance/sessions}}/{name}:/workspace/sessions",
            # Worktrees shared between agents (the create_worktree MCP tool
            # writes here). Outside REPOS_DIR so as not to pollute the status
            # of the canonical repos (which the dev's VSCode/IDE sees).
            "${WORKTREES_DIR:-./instance/worktrees}:/workspace/worktrees",
            # D-60: (Claude Code) hooks shared by all agents.
            # Scripts referenced by hooks_defaults in agents.yaml live here
            # and are available at /app/hooks/ inside the container (ro).
            "${HOOKS_DIR:-./instance/hooks}:/app/hooks:ro",
            "./instance/heartbeats:/heartbeats",
        ],
    }
    if mem_enabled:
        # Memory is now Postgres (memory.facts) — no bind mount needed.
        # DATABASE_URL env var is already set above.
        pass
    # Per-folder mounts (company/repos). "orchestrator" now only keeps company
    # for context; events go through the HTTP broker, not files.
    # Paths expanded by docker compose from the root .env — defaults
    # kept for compat (traditional instance/).
    write = set(agent.get("write_access") or [])
    read = set(agent.get("read_access") or [])
    _MOUNT_SRC = {
        "company": "${COMPANY_DIR:-./instance/company}",
        "repos":   "${REPOS_DIR:-./instance/repos}",
    }
    for mount in ["company", "repos"]:
        src = _MOUNT_SRC[mount]
        if mount == "repos" and "repos" in write:
            # D-115: canonical repos mounted RO + each repo's .git/
            # overlaid RW. `git worktree add`/`fetch`/`prune`/`config`
            # (subprocess inside the agent container — workflow.py)
            # only write to .git/refs, .git/FETCH_HEAD, .git/worktrees/,
            # .git/modules/. The working tree (code, project configs)
            # stays RO in the kernel: the agent can't edit code directly
            # in /workspace/repos/<repo>/<src>, not even via Bash. Legit
            # edits happen in /workspace/worktrees/<repo>/<task>/
            # (separate RW mount, created by the create_worktree MCP).
            svc["volumes"].append(f"{src}:/workspace/{mount}:ro")
            # Repos created by the init_repo tool (web/app/repos.py) keep
            # their git dir in <repos>/.gitdirs/<name>.git. One RW mount
            # covers all of them, including repos created after this
            # container started, which per-repo mounts below can't.
            (REPOS_DIR / ".gitdirs").mkdir(parents=True, exist_ok=True)
            svc["volumes"].append(f"{src}/.gitdirs:/workspace/{mount}/.gitdirs")
            if REPOS_DIR.is_dir():
                for entry in sorted(REPOS_DIR.iterdir()):
                    if entry.name.startswith("."):
                        continue
                    if not (entry / ".git").is_dir():
                        # Skip entries without a .git/ dir: junk, init_repo
                        # repos (.git is a file -> .gitdirs above) or
                        # submodules.
                        continue
                    svc["volumes"].append(
                        f"{src}/{entry.name}/.git:/workspace/{mount}/{entry.name}/.git"
                    )
        elif mount in write:
            svc["volumes"].append(f"{src}:/workspace/{mount}")
        elif mount in read:
            svc["volumes"].append(f"{src}:/workspace/{mount}:ro")
    # Default image -> framework builds it; external image -> no build.
    if not image_override:
        svc["build"] = {
            "context": ".",
            "dockerfile": "framework/docker/agent.Dockerfile",
        }
    # Capabilities with a sidecar MCP become depends_on (ensures ready) and
    # can inject env vars into the agent container (`agent_env`) — needed
    # for stdio servers that take credentials via env (like Sentry).
    for c in agent.get("capabilities") or []:
        spec = resolve_capability(c, capability_instances)
        if spec is None:
            continue
        if spec["service"] is not None:
            svc["depends_on"][f"{c}-mcp"] = {"condition": "service_started"}
        if spec["agent_env"]:
            svc["environment"].update(spec["agent_env"])
    return svc


def write_mcp_extra(agent: dict, capability_instances: dict | None = None) -> None:
    """Write instance/agents/<name>/mcp.extra.json with the capabilities' servers.

    claude_runner merges this file into the mcp-config.json generated for each run.
    """
    name = agent["name"]
    caps = agent.get("capabilities") or []
    servers = {}
    for c in caps:
        spec = resolve_capability(c, capability_instances or {})
        if spec is not None:
            servers[c] = spec["server"]
    import json as _json
    extra_path = AGENTS_DIR / name / "mcp.extra.json"
    if servers:
        extra_path.parent.mkdir(parents=True, exist_ok=True)
        extra_path.write_text(
            _json.dumps({"mcpServers": servers}, indent=2) + "\n",
            encoding="utf-8",
        )
    elif extra_path.exists():
        extra_path.unlink()


def write_override(agents: list[dict], capability_instances: dict | None = None) -> None:
    capability_instances = capability_instances or {}
    services = {f"agent-{a['name']}": build_agent_service(a, capability_instances) for a in agents}
    volumes = {f"agent-{a['name']}-claude": None for a in agents}
    # Collect MCP capabilities used by any agent — one instance of each
    # server serves the N agents that ask for the same capability.
    used_caps: set[str] = set()
    for a in agents:
        used_caps.update(a.get("capabilities") or [])
    for c in sorted(used_caps):
        spec = resolve_capability(c, capability_instances)
        if spec is not None and spec["service"] is not None:
            services[f"{c}-mcp"] = spec["service"]
    content = {"services": services, "volumes": volumes}
    header = textwrap.dedent("""\
        # GENERATED BY framework/scripts/reconcile.py — DO NOT EDIT BY HAND.
        # Source: instance/agents/agents.yaml. Run `./framework/scripts/reconcile.sh` to regenerate.
    """)
    yaml_text = yaml.safe_dump(content, default_flow_style=False, sort_keys=False, allow_unicode=True)
    OVERRIDE_FILE.write_text(header + yaml_text, encoding="utf-8")


# ---------- Per-agent orchestration ----------

def upper_env_prefix(name: str) -> str:
    return f"AGENT_{name.upper().replace('-', '_')}"


def ensure_user_and_subs(client: BrokerAdmin, agent: dict, env: dict[str, str]) -> tuple[str, bool]:
    """Create/update the user (kind=bot) + subscriptions. Returns (api_token, rotated)."""
    name = agent["name"]
    bot_email = f"{name}-bot@internal.ai-company"
    resp = client.upsert_user(
        email=bot_email,
        username=f"{name}-bot",
        full_name=agent["display_name"],
        kind="bot",
        agent_name=name,
    )
    api_token = resp.get("api_token")
    user_id = resp["id"]
    if not api_token:
        # Backend kept the token but didn't return it (legacy edge case) — use .env
        api_token = env.get(f"{upper_env_prefix(name)}_TOKEN", "")
        if not api_token:
            die(f"{name}: user already exists but .env has no token. Delete the user and recreate it, or set {upper_env_prefix(name)}_TOKEN manually.")
    # rotated = .env didn't have this token (first reconcile, or DB recreated
    # with a new token). Containers created earlier must be recreated to
    # pick up the new BROKER_TOKEN.
    prev_token = env.get(f"{upper_env_prefix(name)}_TOKEN", "")
    rotated = api_token != prev_token
    for stream in agent.get("streams") or [name]:
        client.upsert_stream(name=stream, description=f"Stream for agent {name}")
        client.subscribe(user_id=user_id, stream=stream)
    log(f"  {name}: user#{user_id} + {len(agent.get('streams') or [name])} stream(s) ok", "ok")
    return api_token, rotated


# Streams the broker maintains on its own (not from agents.yaml).
# Edit when adding new "system-level" streams configured via env.
RESERVED_STREAMS = {"debug"}


def prune_orphans(client: BrokerAdmin, agents: list[dict], env: dict[str, str]) -> None:
    """Soft-delete broker streams/bot users that are no longer in
    agents.yaml.

    Strategy: try hard-delete first (DELETE). On 409 (has history), fall
    back to soft-delete (PATCH is_active=false). Soft-delete keeps the
    history but removes it from UI listings and from the system prompt's
    team section — a deactivated agent doesn't reappear as a callable peer.

    Whitelist: TERMINAL_NOTIFY_STREAM (se setado) + RESERVED_STREAMS.
    """
    configured_streams: set[str] = set(RESERVED_STREAMS)
    for a in agents:
        configured_streams.update(a.get("streams") or [a["name"]])
    terminal_notify = (os.environ.get("TERMINAL_NOTIFY_STREAM")
                       or env.get("TERMINAL_NOTIFY_STREAM") or "").strip()
    if terminal_notify:
        configured_streams.add(terminal_notify)

    configured_bots = {f"{a['name']}-bot" for a in agents}

    try:
        broker_streams = client.list_streams()
        broker_users = client.list_users()
    except Exception as e:
        log(f"  failed listing broker: {e}", "err")
        return

    orphan_streams = [s for s in broker_streams if s["name"] not in configured_streams]
    for s in orphan_streams:
        name = s["name"]
        code, body = client.delete_stream(name)
        if code == 200:
            log(f"  orphan stream removed (hard-delete): {name}", "ok")
        elif code == 409:
            # Has conversation history. Soft-delete keeps and hides it.
            if s.get("is_active") is False:
                log(f"  stream {name!r} already inactive", "ok")
            else:
                pcode, pbody = client.set_stream_active(name, False)
                if pcode == 200:
                    log(f"  orphan stream deactivated (soft-delete): {name}", "ok")
                else:
                    log(f"  failed deactivating stream {name!r}: HTTP {pcode} {pbody[:120]}", "err")
        else:
            log(f"  failed removing stream {name!r}: HTTP {code} {body[:120]}", "err")

    orphan_bots = [u for u in broker_users
                   if u.get("kind") == "bot"
                   and u.get("agent_name")
                   and u["username"] not in configured_bots
                   and u["username"] != "system-bot"]
    for u in orphan_bots:
        username = u["username"]
        code, body = client.delete_user(username)
        if code == 200:
            log(f"  orphan bot removed (hard-delete): {username}", "ok")
        elif code == 409:
            if u.get("is_active") is False:
                log(f"  user {username!r} already inactive", "ok")
            else:
                pcode, pbody = client.set_user_active(username, False)
                if pcode == 200:
                    log(f"  orphan bot deactivated (soft-delete): {username}", "ok")
                else:
                    log(f"  failed deactivating user {username!r}: HTTP {pcode} {pbody[:120]}", "err")
        else:
            log(f"  failed removing user {username!r}: HTTP {code} {body[:120]}", "err")

    if not orphan_streams and not orphan_bots:
        log("  nothing to remove", "ok")


# ---------- Main ----------

def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile agents.yaml -> actual state")
    parser.add_argument("--dry-run", action="store_true", help="only show what it would do")
    parser.add_argument("--no-up", action="store_true", help="don't run docker compose up at the end")
    parser.add_argument("--no-broker", action="store_true", help="skip broker users/streams (FS + override only)")
    args = parser.parse_args()

    if not AGENTS_YAML.exists():
        die(f"{AGENTS_YAML} does not exist. Create it from framework/examples/agents.yaml.example.")

    log("Ensuring instance directories (keeps Docker from creating them as root)", "step")
    # Folders customizable via env (AGENTS_DIR, COMPANY_DIR, BACKUPS_DIR, REPOS_DIR)
    # or default under instance/. Heartbeats stays in instance/ (ephemeral,
    # no use case for moving it).
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    COMPANY_DIR.mkdir(parents=True, exist_ok=True)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    (INSTANCE_DIR / "repos").mkdir(parents=True, exist_ok=True)
    (INSTANCE_DIR / "heartbeats").mkdir(parents=True, exist_ok=True)
    # Worktrees must be writable by the container (UID node=1000) and readable
    # by the host (the dev's UID). chmod 777 avoids the clash — same problem the
    # repos/ mount had before (host UID 1001 vs the container's node).
    try:
        WORKTREES_DIR.chmod(0o777)
    except OSError:
        pass

    # System prompts (D-63): folder + config.yaml seed. platform.md moved out
    # of here — it became a framework invariant and lives inside the images
    # (agent/web), via COPY framework/system_prompts. The instance only seeds
    # config.yaml (toggles editable from the PWA).
    sp_dir = COMPANY_DIR / "system_prompts"
    sp_dir.mkdir(parents=True, exist_ok=True)
    sp_examples = PROJECT_ROOT / "framework" / "examples" / "system_prompts"
    for fname in ("config.yaml",):
        target = sp_dir / fname
        example = sp_examples / f"{fname}.example"
        if not target.exists() and example.exists():
            target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            try:
                shown = target.relative_to(PROJECT_ROOT)
            except ValueError:
                shown = target  # COMPANY_DIR outside PROJECT_ROOT
            log(f"  seed: {shown} copied from example", "ok")
    # Migration cleanup: old instances seeded platform.md here. The file now
    # lives in the image; removing the orphan avoids confusion (the PWA showed
    # the size of the instance file, which the runner was ignoring).
    legacy_platform = sp_dir / "platform.md"
    if legacy_platform.exists():
        legacy_platform.unlink()
        try:
            shown = legacy_platform.relative_to(PROJECT_ROOT)
        except ValueError:
            shown = legacy_platform
        log(f"  removed legacy {shown} (platform.md is now framework-fixed)", "ok")

    log("Parsing agents.yaml", "step")
    agents_text = AGENTS_YAML.read_text(encoding="utf-8")
    if LEGACY_MCP_PREFIX in agents_text:
        # Pre-rename configs: the MCP server used to be called agent_framework.
        agents_text = agents_text.replace(LEGACY_MCP_PREFIX, MCP_PREFIX)
        log(
            f"agents.yaml still uses {LEGACY_MCP_PREFIX}* — treating it as {MCP_PREFIX}*. "
            f"Update the file: sed -i 's/{LEGACY_MCP_PREFIX}/{MCP_PREFIX}/g' {AGENTS_YAML}",
            "warn",
        )
    data = yaml.safe_load(agents_text)
    agents, hooks_defaults, capability_instances = validate_schema(data)
    log(f"{len(agents)} agent(s) in config: {[a['name'] for a in agents]}", "ok")
    if hooks_defaults:
        log(f"hooks_defaults: {sorted(hooks_defaults.keys())}", "ok")
    if capability_instances:
        log(
            f"capability_instances: {sorted(capability_instances)}",
            "ok",
        )

    env = load_env(ENV_FILE)
    broker_url = os.environ.get("BROKER_URL") or env.get("BROKER_URL") or "http://web:8090"
    admin_token = os.environ.get("ORCHESTRATOR_TOKEN") or env.get("ORCHESTRATOR_TOKEN")
    if not admin_token:
        die("ORCHESTRATOR_TOKEN missing from env/.env — reconcile uses it to authenticate with the broker.")

    client = None
    if not args.no_broker:
        log("Checking broker connectivity", "step")
        client = BrokerAdmin(broker_url, admin_token)
        try:
            client._get("/api/users/me")
            log(f"broker ok at {broker_url}", "ok")
        except Exception as e:
            die(f"Could not reach broker at {broker_url}: {e}")

    new_env: dict[str, str] = {}
    rotated_agents: list[str] = []
    for a in agents:
        log(f"\n== Agent: {a['name']} ({a['display_name']}) ==", "step")
        if args.dry_run:
            log("  (dry-run, skipping actions)", "info")
            continue
        ensure_agent_dir(a, hooks_defaults=hooks_defaults)
        write_mcp_extra(a, capability_instances)
        log(f"  filesystem: {AGENTS_DIR}/{a['name']}/ ok", "ok")
        if client is not None:
            try:
                token, rotated = ensure_user_and_subs(client, a, env)
                if token:
                    new_env[f"{upper_env_prefix(a['name'])}_TOKEN"] = token
                if rotated:
                    rotated_agents.append(a["name"])
            except Exception as e:
                log(f"  {a['name']}: broker failure: {e}", "err")
                raise

    if client is not None and not args.dry_run:
        log("\nPruning orphan streams/users in the broker", "step")
        prune_orphans(client, agents, env)

    if new_env and not args.dry_run:
        log("\nUpdating .env", "step")
        upsert_env(ENV_FILE, new_env)
        log(f"{len(new_env)} var(s) in .env", "ok")

    log("\nGenerating docker-compose.override.yml", "step")
    if args.dry_run:
        log("(dry-run, not writing)", "info")
    else:
        write_override(agents, capability_instances)
        log(f"{OVERRIDE_FILE.name} written with {len(agents)} service(s)", "ok")

    if args.no_up:
        print("__RECONCILE_NO_UP__")
    elif not args.dry_run:
        print("__RECONCILE_DO_UP__")
        if rotated_agents:
            # Flag agents whose token was just created — the wrapper recreates
            # their containers so they pick up the updated env.
            print(f"__RECONCILE_ROTATED__ {' '.join(rotated_agents)}")

    log("\nreconcile complete.", "step")
    return 0


if __name__ == "__main__":
    sys.exit(main())
