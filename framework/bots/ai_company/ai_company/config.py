"""Agent config: combines env vars + agent.yaml.

Required env:
  AGENT_NAME                — agent name (matches the agents/<name>/ subfolder)
  BROKER_URL                — internal broker URL (e.g. http://web:8090)
  BROKER_TOKEN              — Bearer token the bot uses to auth with the broker
  DATABASE_URL              — postgres://... (for LISTEN/NOTIFY + memory)

Optional (with defaults):
  POOL_SIZE                 — overrides pool_size from agent.yaml
  LOG_LEVEL                 — INFO by default
  WORKSPACE_REPOS           — /workspace/repos
  WORKTREES_DIR             — /workspace/worktrees (separate from WORKSPACE_REPOS
                              to avoid dirtying the canonical repo's status; read
                              by WorkflowManager.create_worktree)
  WORKSPACE_COMPANY         — /workspace/company
  WORKSPACE_AGENT           — /app/agents/<name>
  WORKSPACE_SESSIONS        — /workspace/sessions (D-51: root of the per-topic cwds)
  CLAUDE_HOME               — /home/node/.claude
  TRANSCRIBER_URL           — empty disables auto-transcription (e.g. http://transcriber:8000)
  TRANSCRIBER_LANGUAGE      — empty = auto-detect (e.g. "pt")
  TELEMETRY_URL             — empty disables sending telemetry (e.g. http://web:8090/api/telemetry/event)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import yaml


@dataclass(frozen=True)
class BrokerConfig:
    url: str
    token: str
    database_url: str


# Default tools: only the MCP ask_human tool. Agents that need file
# I/O must override this in agent.yaml with allowed_tools: [Read, Write, Edit, ...].
DEFAULT_ALLOWED_TOOLS: list[str] = ["mcp__ai_company__ask_human"]

# The MCP server was called agent_framework before the rename; accept tool
# names from older agent.yaml files.
LEGACY_MCP_PREFIX = "mcp__agent_framework__"


def _normalize_tool(name: str) -> str:
    if name.startswith(LEGACY_MCP_PREFIX):
        return "mcp__ai_company__" + name[len(LEGACY_MCP_PREFIX):]
    return name


@dataclass(frozen=True)
class AgentConfig:
    name: str
    streams: list[str]
    pool_size: int
    idle_timeout_sec: int
    allowed_tools: list[str]
    broker: BrokerConfig
    agent_home: Path
    workspace_repos: Path
    workspace_company: Path
    workspace_sessions: Path
    workspace_orchestrator: Path
    claude_home: Path
    log_level: str
    transcriber_url: str | None
    transcriber_language: str | None
    telemetry_url: str | None
    memory_enabled: bool
    memory_auto_inject_limit: int     # 0 = tools available but no auto-inject
    model: str | None                 # None = use the claude CLI default
    effort: str | None                # None = no flag; values: low|medium|high|xhigh|max
    main_repo: str | None             # name of the repo in workspace_repos/ whose .claude/ is merged into the session_dir (D-64)

    @classmethod
    def from_env(cls) -> "AgentConfig":
        name = _req_env("AGENT_NAME")
        agent_home = Path(os.environ.get("WORKSPACE_AGENT", f"/app/agents/{name}"))

        data: dict = {}
        agent_yaml = agent_home / "agent.yaml"
        if agent_yaml.exists():
            loaded = yaml.safe_load(agent_yaml.read_text()) or {}
            if isinstance(loaded, dict):
                data = loaded

        tools = data.get("allowed_tools")
        allowed_tools = [_normalize_tool(t) for t in tools] if tools else list(DEFAULT_ALLOWED_TOOLS)

        # model + effort + thinking
        model = data.get("model") or None
        effort = data.get("effort") or None
        thinking = data.get("thinking")
        # thinking=true without an explicit effort becomes --effort high (shortcut)
        if thinking and not effort:
            effort = "high"
        # Validate effort if specified
        if effort and effort not in ("low", "medium", "high", "xhigh", "max"):
            raise RuntimeError(
                f"invalid effort: {effort!r}. Use low|medium|high|xhigh|max"
            )

        # memory config: object {enabled, auto_inject_limit}, or bool shortcut
        mem_cfg = data.get("memory")
        if mem_cfg is None:
            memory_enabled = True
            memory_auto_inject_limit = 5
        elif isinstance(mem_cfg, bool):
            memory_enabled = mem_cfg
            memory_auto_inject_limit = 5 if mem_cfg else 0
        elif isinstance(mem_cfg, dict):
            memory_enabled = bool(mem_cfg.get("enabled", True))
            memory_auto_inject_limit = int(mem_cfg.get("auto_inject_limit", 5))
        else:
            memory_enabled = True
            memory_auto_inject_limit = 5

        return cls(
            name=name,
            streams=list(data.get("streams") or [f"#{name}"]),
            pool_size=int(os.environ.get("POOL_SIZE") or data.get("pool_size", 2)),
            idle_timeout_sec=int(os.environ.get("IDLE_TIMEOUT_SEC") or data.get("idle_timeout_sec", 900)),
            allowed_tools=allowed_tools,
            broker=BrokerConfig(
                url=_req_env("BROKER_URL"),
                token=_req_env("BROKER_TOKEN"),
                database_url=_req_env("DATABASE_URL"),
            ),
            agent_home=agent_home,
            workspace_repos=Path(os.environ.get("WORKSPACE_REPOS", "/workspace/repos")),
            workspace_company=Path(os.environ.get("WORKSPACE_COMPANY", "/workspace/company")),
            workspace_sessions=Path(os.environ.get("WORKSPACE_SESSIONS", "/workspace/sessions")),
            workspace_orchestrator=Path(os.environ.get("WORKSPACE_ORCHESTRATOR", "/workspace/orchestrator")),
            claude_home=Path(os.environ.get("CLAUDE_HOME", "/home/node/.claude")),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            transcriber_url=(os.environ.get("TRANSCRIBER_URL") or None),
            transcriber_language=(os.environ.get("TRANSCRIBER_LANGUAGE") or None),
            telemetry_url=(os.environ.get("TELEMETRY_URL") or None),
            memory_enabled=memory_enabled,
            memory_auto_inject_limit=memory_auto_inject_limit,
            model=model,
            effort=effort,
            main_repo=(data.get("main_repo") or None),
        )


def _req_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Required env var not set: {name}")
    return v
