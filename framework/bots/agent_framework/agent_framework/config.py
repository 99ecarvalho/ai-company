"""Config do agente: mistura env vars + agent.yaml.

Env obrigatorios:
  AGENT_NAME                — nome do agente (casa com subpasta agents/<name>/)
  BROKER_URL                — URL do broker interno (ex: http://web:8090)
  BROKER_TOKEN              — Bearer token de auth do bot no broker
  DATABASE_URL              — postgres://... (pra LISTEN/NOTIFY + memory)

Opcionais (com defaults):
  POOL_SIZE                 — override do pool_size em agent.yaml
  LOG_LEVEL                 — INFO por padrao
  WORKSPACE_REPOS           — /workspace/repos
  WORKTREES_DIR             — /workspace/worktrees (isolado de WORKSPACE_REPOS
                              pra evitar sujar status do repo canonico; lido
                              por WorkflowManager.create_worktree)
  WORKSPACE_COMPANY         — /workspace/company
  WORKSPACE_AGENT           — /app/agents/<name>
  WORKSPACE_SESSIONS        — /workspace/sessions (D-51: root dos cwds por topic)
  CLAUDE_HOME               — /home/node/.claude
  TRANSCRIBER_URL           — vazio desliga auto-transcricao (ex: http://transcriber:8000)
  TRANSCRIBER_LANGUAGE      — vazio = auto-detect (ex: "pt")
  TELEMETRY_URL             — vazio desliga envio de telemetria (ex: http://web:8090/api/telemetry/event)
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


# Tools default: apenas a tool ask_human do MCP. Agentes que precisam de file
# I/O devem sobrescrever em agent.yaml com allowed_tools: [Read, Write, Edit, ...].
DEFAULT_ALLOWED_TOOLS: list[str] = ["mcp__agent_framework__ask_human"]


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
    memory_auto_inject_limit: int     # 0 = tools disponiveis mas sem auto-inject
    model: str | None                 # None = usa default do claude CLI
    effort: str | None                # None = sem flag; valores: low|medium|high|xhigh|max
    main_repo: str | None             # nome do repo em workspace_repos/ cujo .claude/ eh mergeado no session_dir (D-64)

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
        allowed_tools = list(tools) if tools else list(DEFAULT_ALLOWED_TOOLS)

        # model + effort + thinking
        model = data.get("model") or None
        effort = data.get("effort") or None
        thinking = data.get("thinking")
        # thinking=true sem effort explicito vira --effort high (atalho)
        if thinking and not effort:
            effort = "high"
        # Valida effort se especificado
        if effort and effort not in ("low", "medium", "high", "xhigh", "max"):
            raise RuntimeError(
                f"effort invalido: {effort!r}. Use low|medium|high|xhigh|max"
            )

        # memory config: objeto {enabled, auto_inject_limit}, ou bool shortcut
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
        raise RuntimeError(f"Env var obrigatoria nao setada: {name}")
    return v
