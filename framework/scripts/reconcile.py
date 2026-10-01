#!/usr/bin/env python3
"""Reconcile de instance/agents/agents.yaml -> estado real.

Idempotente:
  1. Parse + validacao de schema
  2. Pra cada agente:
     a. Cria user (kind=bot) no broker interno → recebe api_token → salva no .env
     b. Cria stream(s) + subscreve o bot
     c. Garante instance/agents/<name>/ (CLAUDE.md do template se faltar; knowledge/, pending_questions/)
     d. Escreve instance/agents/<name>/agent.yaml derivado
  3. Gera docker-compose.override.yml (um service por agente)

Run:
  framework/scripts/reconcile.sh                # aplica + docker compose up -d
  framework/scripts/reconcile.sh --dry-run
  framework/scripts/reconcile.sh --no-broker    # pula criacao de users/streams (so FS + override)
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


# ---------- Constantes ----------

PROJECT_ROOT = Path(os.environ.get("PROJECT_ROOT", ".")).resolve()
INSTANCE_DIR = PROJECT_ROOT / "instance"
ENV_FILE = PROJECT_ROOT / ".env"
TEMPLATE_DIR = PROJECT_ROOT / "framework" / "agent-template"
OVERRIDE_FILE = PROJECT_ROOT / "docker-compose.override.yml"


def _env_path(var: str, default_rel: str) -> Path:
    """Resolve path de um env var (em .env ou ambiente), caindo em
    PROJECT_ROOT/<default_rel> se ausente. Paths relativos sao ancorados
    em PROJECT_ROOT (matches docker-compose semantics)."""
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
REPOS_DIR = _env_path("REPOS_DIR", "instance/repos")  # D-115: enumerar subdirs com .git/ pra split mount
AGENTS_YAML = AGENTS_DIR / "agents.yaml"

VALID_MOUNTS = {"company", "orchestrator", "repos"}
MCP_PREFIX = "mcp__ai_company__"
LEGACY_MCP_PREFIX = "mcp__agent_framework__"
AGENT_IMAGE = "ai-company/agent:0.1.0"

NAME_MAX_LEN = 31  # 1 inicial + ate 30 subsequentes. Cap generoso pra DNS/stream.
NAME_RE = re.compile(rf"^[a-z][a-z0-9-]{{0,{NAME_MAX_LEN - 1}}}$")

# Capabilities MCP em duas categorias (D-119):
#
# (1) MCP_CAPABILITIES — singletons pre-definidos no framework. Usados por
#     nome direto em `agent.capabilities`. Pra capabilities que naturalmente
#     nao tem multiplas instancias por instalacao (ex: token Sentry por org,
#     pool Playwright unico). Dois shapes:
#       - HTTP sidecar: `service` (Compose service) + `server` apontando pra
#         URL do sidecar. Reconcile injeta o service no override e adiciona
#         depends_on nos agentes que declaram a capability.
#       - In-process stdio: so `server` com type: stdio (sem `service`).
#         Claude Code spawna no container do agente.
#     `agent_env` (opcional) lista env vars injetadas no container do agente.
#
# (2) CAPABILITY_TEMPLATES — templates parametrizaveis. A instancia declara
#     em `agents.yaml` -> `capability_instances` o nome + env values.
#     Permite ter `mysql-producao` + `mysql-staging` com creds diferentes,
#     sem leak de vocabulario da instancia pro framework. Stdio-only por
#     enquanto (HTTP sidecar parametrizavel = v2 quando necessario).
#
# Resolucao via `resolve_capability(name, instances)` — instances ganha
# precedencia sobre singletons em caso de colisao de nome.

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
        # @benborla29/mcp-server-mysql: stdio nativo, le creds via env.
        # Read-only por default (ALLOW_*_OPERATION unset).
        "server": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@benborla29/mcp-server-mysql"],
        },
        # Env vars que o MCP server le. Validador rejeita keys fora desta
        # lista na instance (cata typo). Instances usam template-string
        # Compose-style (`${DB_FOO:-}`) que reconcile passa pro container
        # do agente em build_agent_service; Compose interpola em up-time.
        "env_keys": ["MYSQL_HOST", "MYSQL_PORT", "MYSQL_USER", "MYSQL_PASS", "MYSQL_DB"],
    },
    "sentry": {
        # @sentry/mcp-server: stdio nativo. Roda no proprio container do
        # agente via npx — sem sidecar. Token escopado por org Sentry; host
        # vazio = SaaS sentry.io, preenchido = self-hosted.
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
    """Retorna spec unificada da capability ou None se desconhecida.

    Shape: {server, service|None, agent_env}. Instances tem precedencia
    sobre singletons em caso de colisao de nome.
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
        out.append("# Agents (gerado por reconcile)")
        for k in new_keys:
            out.append(f"{k}={kv[k]}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


# ---------- Schema validation ----------

def _validate_hooks_block(value: Any, ctx: str) -> None:
    """Valida estrutura de um bloco `hooks` (defaults ou per-agent).

    Passthrough pra settings.json do Claude Code — ver
    https://code.claude.com/docs/en/hooks-guide. Validamos a FORMA (mapping
    de event->list de groups com `hooks: [...]`), nao o conteudo semantico
    (nomes de eventos, tipos de hook, matchers) — o CLI valida ao carregar
    e o conjunto de eventos evolui.
    """
    if value is None:
        return
    if not isinstance(value, dict):
        die(f"{ctx}: `hooks` deve ser mapping event->list")
    for event, groups in value.items():
        if not isinstance(event, str) or not event:
            die(f"{ctx}: chave de `hooks` deve ser nome de evento (string nao-vazia), got {event!r}")
        if not isinstance(groups, list):
            die(f"{ctx}.hooks.{event}: deve ser lista de groups")
        for j, g in enumerate(groups):
            gctx = f"{ctx}.hooks.{event}[{j}]"
            if not isinstance(g, dict):
                die(f"{gctx}: deve ser mapping")
            inner = g.get("hooks")
            if not isinstance(inner, list) or not inner:
                die(f"{gctx}: campo `hooks` (lista de commands) obrigatorio e nao-vazio")
            for k, h in enumerate(inner):
                hctx = f"{gctx}.hooks[{k}]"
                if not isinstance(h, dict) or not isinstance(h.get("type"), str):
                    die(f"{hctx}: precisa ter `type` (string)")


def merge_hooks(defaults: dict | None, per_agent: dict | None) -> dict:
    """Merge hooks_defaults + agent.hooks concatenando por evento.

    Semantica: lista de groups de defaults vem primeiro, agent depois. Cada
    group e preservado intacto (Claude Code trata cada group independentemente
    — matchers nao se sobrepoem). Eventos soh em um dos lados aparecem como
    estao no resultado.
    """
    out: dict[str, list] = {}
    for src in (defaults or {}, per_agent or {}):
        for event, groups in src.items():
            out.setdefault(event, []).extend(groups)
    return out


def _validate_capability_instances(value: Any) -> dict:
    """Valida bloco top-level `capability_instances` (D-119).

    Shape:
        capability_instances:
          <instance-name>:
            template: <template-name in CAPABILITY_TEMPLATES>
            env: { KEY: VALUE-OR-COMPOSE-TEMPLATE-STRING, ... }

    instance-name vira o namespace MCP (mcp__<name>__*) e tambem o nome do
    server em mcp.extra.json. env keys sao validadas contra o template.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        die("`capability_instances` deve ser mapping name->spec")
    out: dict[str, dict] = {}
    for name, spec in value.items():
        ctx = f"capability_instances.{name}"
        if not isinstance(name, str) or not NAME_RE.match(name):
            die(f"{ctx}: nome invalido (deve casar /^[a-z][a-z0-9-]*$/)")
        if not isinstance(spec, dict):
            die(f"{ctx}: deve ser mapping com `template` + `env`")
        tpl_name = spec.get("template")
        if not isinstance(tpl_name, str):
            die(f"{ctx}.template: obrigatorio (string)")
        tpl = CAPABILITY_TEMPLATES.get(tpl_name)
        if tpl is None:
            die(
                f"{ctx}.template: template desconhecido {tpl_name!r}. "
                f"Conhecidos: {sorted(CAPABILITY_TEMPLATES)}"
            )
        env = spec.get("env") or {}
        if not isinstance(env, dict):
            die(f"{ctx}.env: deve ser mapping KEY->value")
        valid_keys = set(tpl.get("env_keys") or [])
        for k, v in env.items():
            if not isinstance(k, str) or not k:
                die(f"{ctx}.env: keys devem ser strings nao-vazias")
            if valid_keys and k not in valid_keys:
                die(
                    f"{ctx}.env.{k}: chave nao reconhecida pelo template "
                    f"{tpl_name!r}. Aceitas: {sorted(valid_keys)}"
                )
            if not isinstance(v, (str, int)):
                die(f"{ctx}.env.{k}: deve ser string (suporta ${{VAR:-default}})")
        # Colisao com singleton: instances vence (resolve_capability ja faz isso),
        # mas avisa pra evitar surpresa.
        if name in MCP_CAPABILITIES:
            log(
                f"{ctx}: instance overrides singleton de mesmo nome em MCP_CAPABILITIES",
                "warn",
            )
        out[name] = {"template": tpl_name, "env": {k: str(v) for k, v in env.items()}}
    return out


def validate_schema(data: dict) -> tuple[list[dict], dict, dict]:
    if not isinstance(data, dict):
        die("agents.yaml deve ser um mapping no topo")
    if data.get("schema_version") != 1:
        die("schema_version precisa ser 1")
    hooks_defaults = data.get("hooks_defaults") or {}
    _validate_hooks_block(hooks_defaults, "hooks_defaults")
    capability_instances = _validate_capability_instances(data.get("capability_instances"))
    known_caps = known_capabilities(capability_instances)
    agents = data.get("agents") or []
    if not isinstance(agents, list):
        die("`agents` deve ser lista")
    seen_names: set[str] = set()
    seen_streams: set[str] = set()
    for i, a in enumerate(agents):
        ctx = f"agents[{i}]"
        if not isinstance(a, dict):
            die(f"{ctx}: deve ser um mapping")
        name = a.get("name")
        if not name:
            die(f"{ctx}: `name` obrigatorio")
        if not NAME_RE.match(name):
            if len(name) > NAME_MAX_LEN:
                die(f"{ctx}: `name` invalido: {name!r} tem {len(name)} chars "
                    f"(maximo {NAME_MAX_LEN}). Regex: ^[a-z][a-z0-9-]{{0,{NAME_MAX_LEN - 1}}}$")
            die(f"{ctx}: `name` invalido: {name!r}. "
                f"Deve comecar com [a-z] e conter apenas [a-z0-9-], ate {NAME_MAX_LEN} chars.")
        if name in seen_names:
            die(f"{ctx}: nome duplicado: {name!r}")
        seen_names.add(name)
        if not a.get("display_name"):
            die(f"{ctx}: `display_name` obrigatorio")
        if not a.get("description"):
            die(f"{ctx}: `description` obrigatorio")
        streams = a.get("streams") or [name]
        if not isinstance(streams, list) or not streams:
            die(f"{ctx}: `streams` deve ser lista nao-vazia")
        for s in streams:
            if s in seen_streams:
                die(f"{ctx}: stream duplicada: {s!r}")
            seen_streams.add(s)
        for key in ("write_access", "read_access"):
            val = a.get(key) or []
            if not isinstance(val, list):
                die(f"{ctx}: `{key}` deve ser lista")
            for m in val:
                if m not in VALID_MOUNTS:
                    die(f"{ctx}: `{key}` mount desconhecido: {m!r}")
        overlap = set(a.get("write_access") or []) & set(a.get("read_access") or [])
        if overlap:
            die(f"{ctx}: overlap write/read: {overlap}")
        pool = a.get("pool_size", 2)
        if not isinstance(pool, int) or pool < 1:
            die(f"{ctx}: `pool_size` deve ser int >= 1")
        idle = a.get("idle_timeout_sec", 900)
        if not isinstance(idle, int) or idle < 1:
            die(f"{ctx}: `idle_timeout_sec` deve ser int >= 1")
        model = a.get("model")
        if model is not None and not isinstance(model, str):
            die(f"{ctx}: `model` deve ser string")
        effort = a.get("effort")
        if effort is not None and effort not in ("low", "medium", "high", "xhigh", "max"):
            die(f"{ctx}: `effort` invalido: {effort!r}")
        caps = a.get("capabilities") or []
        if not isinstance(caps, list):
            die(f"{ctx}: `capabilities` deve ser lista")
        for c in caps:
            if c not in known_caps:
                die(
                    f"{ctx}: capability desconhecida: {c!r}. "
                    f"Conhecidas: {sorted(known_caps)}"
                )
        image = a.get("image")
        if image is not None and not isinstance(image, str):
            die(f"{ctx}: `image` deve ser string (ex: registry.gitlab.com/acme/my-agent:v1)")
        _validate_hooks_block(a.get("hooks"), ctx)
    return agents, hooks_defaults, capability_instances


# ---------- Broker admin client ----------

class BrokerAdmin:
    """HTTP client pro broker interno."""

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
        # Broker retorna {streams: [...], default: ...} pro PWA
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
# Gerado por reconcile a partir de instance/agents/agents.yaml.
# Edicoes manuais aqui sao sobrescritas no proximo reconcile.
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
    # Sessions (runtime cwds por topic) moram em ${SESSIONS_DIR}/<name>/ —
    # fora de agent_home pra separar runtime efemero de config persistente (D-51).
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
        log(f"  CLAUDE.md criado ({name})", "ok")

    # .claude/settings.json — espelha allowed_tools em permissions.allow.
    # Claude Code (CLI, -p mode) le isso ao iniciar a session. Sem isso, tools
    # MCP nossas viram permission-prompt que morre em non-interactive.
    claude_settings_dir = d / ".claude"
    claude_settings_dir.mkdir(exist_ok=True)
    allowed = list(agent.get("allowed_tools") or [])
    settings: dict = {"permissions": {"allow": allowed}}
    # Hooks do Claude Code: merge de hooks_defaults (top-level) + agent.hooks,
    # concatenados por evento. Passthrough pra settings.json — o CLI valida
    # e executa. Hook scripts precisam usar paths absolutos internos ao
    # container (ex: /app/agents/<name>/hooks/foo.sh). jq esta disponivel
    # na imagem agent (framework/docker/agent.Dockerfile).
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
    # description quotada com json.dumps: JSON string eh YAML valido e
    # escapa aspas/caracteres especiais corretamente. Sem isso, descricoes
    # com ":" (comum em PT-BR: "Fase 1:...", "Consultivo:...") quebram o
    # parse YAML no bot (interpretado como mapping key).
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
    # BYOI: agente pode apontar pra imagem propria (registry publico/privado).
    # Nesse caso nao fazemos build — usuario e responsavel pela imagem.
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
            # Azure AI Foundry (Claude via Azure) — se preenchido, o CLI usa
            # este provider em vez do Anthropic direto.
            "CLAUDE_CODE_USE_FOUNDRY": "${CLAUDE_CODE_USE_FOUNDRY:-}",
            "ANTHROPIC_FOUNDRY_RESOURCE": "${ANTHROPIC_FOUNDRY_RESOURCE:-}",
            "ANTHROPIC_FOUNDRY_API_KEY": "${ANTHROPIC_FOUNDRY_API_KEY:-}",
            "POOL_SIZE": "${POOL_SIZE:-}",
            "IDLE_TIMEOUT_SEC": "${IDLE_TIMEOUT_SEC:-}",
            "TZ": "${TZ:-America/Sao_Paulo}",
            # Git push / MR-PR — usados por executores e revisor-testador.
            # Ficam no env de todos os agentes; agentes consultivos/analistas
            # simplesmente nao os invocam (CLAUDE.md define a disciplina).
            "GITLAB_TOKEN": "${GITLAB_TOKEN:-}",
            "GITLAB_HOST": "${GITLAB_HOST:-}",
            "GH_TOKEN": "${GH_TOKEN:-}",
            "GIT_AUTHOR_NAME": "${GIT_AUTHOR_NAME:-ai-company}",
            "GIT_AUTHOR_EMAIL": "${GIT_AUTHOR_EMAIL:-agents@local}",
            # glab lê GITLAB_TOKEN por padrão, mas também aceita GLAB_TOKEN.
            # gh aceita GH_TOKEN diretamente. Nada mais a configurar no runtime.
        },
        "volumes": [
            # D-90: dir bind do ~/.claude/ inteiro do host (read-only) num path
            # de staging. Diretorios resolvem por path no kernel — quando o
            # Claude CLI do host refresca .credentials.json (write-then-rename
            # = novo inode), o container ve a versao nova na proxima leitura.
            # Pre-spawn sync no claude_runner.py copia pro volume do agente.
            # Bind de arquivo (anterior) congelava no inode antigo e gerava o
            # 401 silencioso descrito em D-55.
            "${HOME}/.claude/:/tmp/host-claude/:ro",
            # .claude.json segue file bind (caso menos critico, rotacao rara).
            # Se virar problema, mesma solucao: mount de ~/ ou symlink no host.
            "${HOME}/.claude.json:/tmp/claude-auth/.claude.json:ro",
            f"agent-{name}-claude:/home/node/.claude",
            f"${{AGENTS_DIR:-./instance/agents}}/{name}:/app/agents/{name}",
            # D-51: sessions (runtime cwds por topic) fora de AGENTS_DIR.
            # Separa config persistente do agente (agent.yaml, CLAUDE.md,
            # knowledge/) do state efemero (session_id, symlinks, GC 24h).
            f"${{SESSIONS_DIR:-./instance/sessions}}/{name}:/workspace/sessions",
            # Worktrees compartilhadas entre agentes (create_worktree MCP tool
            # grava aqui). Fora de REPOS_DIR pra nao poluir status dos repos
            # canonicos (que o VSCode/IDE do dev enxerga).
            "${WORKTREES_DIR:-./instance/worktrees}:/workspace/worktrees",
            # D-60: hooks compartilhados (Claude Code) entre todos os agentes.
            # Scripts referenciados por hooks_defaults em agents.yaml moram aqui
            # e ficam acessiveis em /app/hooks/ dentro do container (ro).
            "${HOOKS_DIR:-./instance/hooks}:/app/hooks:ro",
            "./instance/heartbeats:/heartbeats",
        ],
    }
    if mem_enabled:
        # Memoria agora e Postgres (memory.facts) — nao precisa de bind mount.
        # Env var DATABASE_URL ja setada acima.
        pass
    # Mounts por pasta (company/repos). "orchestrator" agora so mantem company
    # pra contexto; eventos vao pelo broker HTTP, nao por arquivo.
    # Paths expandidos pelo docker compose a partir do .env na raiz — defaults
    # preservados pra compat (instance/ tradicional).
    write = set(agent.get("write_access") or [])
    read = set(agent.get("read_access") or [])
    _MOUNT_SRC = {
        "company": "${COMPANY_DIR:-./instance/company}",
        "repos":   "${REPOS_DIR:-./instance/repos}",
    }
    for mount in ["company", "repos"]:
        src = _MOUNT_SRC[mount]
        if mount == "repos" and "repos" in write:
            # D-115: repos canonicos montados RO + .git/ de cada repo
            # sobreposto RW. `git worktree add`/`fetch`/`prune`/`config`
            # (subprocess dentro do container do agente — workflow.py)
            # so escrevem em .git/refs, .git/FETCH_HEAD, .git/worktrees/,
            # .git/modules/. Working tree (codigo, configs do projeto)
            # fica RO no kernel: agente nao consegue editar codigo direto
            # em /workspace/repos/<repo>/<src>, mesmo via Bash. Edicao
            # legitima acontece em /workspace/worktrees/<repo>/<task>/
            # (mount RW separado, criado pelo create_worktree MCP).
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
                        # Pula entries sem .git/ dir: lixo, repos do
                        # init_repo (.git eh arquivo -> .gitdirs acima) ou
                        # submodulos.
                        continue
                    svc["volumes"].append(
                        f"{src}/{entry.name}/.git:/workspace/{mount}/{entry.name}/.git"
                    )
        elif mount in write:
            svc["volumes"].append(f"{src}:/workspace/{mount}")
        elif mount in read:
            svc["volumes"].append(f"{src}:/workspace/{mount}:ro")
    # Imagem default -> framework builda; imagem externa -> sem build.
    if not image_override:
        svc["build"] = {
            "context": ".",
            "dockerfile": "framework/docker/agent.Dockerfile",
        }
    # Capabilities com MCP lateral entram como depends_on (garante ready) e
    # podem injetar env vars no container do agente (`agent_env`) — necessário
    # pra stdio servers que precisam de credenciais via env (tipo Sentry).
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
    """Escreve instance/agents/<name>/mcp.extra.json com servers das capabilities.

    Claude_runner faz merge desse arquivo no mcp-config.json gerado pra cada run.
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
    # Agrupa capabilities MCP usadas por qualquer agente — uma instancia de
    # cada server serve N agentes que pedirem a mesma capability.
    used_caps: set[str] = set()
    for a in agents:
        used_caps.update(a.get("capabilities") or [])
    for c in sorted(used_caps):
        spec = resolve_capability(c, capability_instances)
        if spec is not None and spec["service"] is not None:
            services[f"{c}-mcp"] = spec["service"]
    content = {"services": services, "volumes": volumes}
    header = textwrap.dedent("""\
        # GERADO POR framework/scripts/reconcile.py — NAO EDITE MANUALMENTE.
        # Fonte: instance/agents/agents.yaml. Rode `./framework/scripts/reconcile.sh` pra regenerar.
    """)
    yaml_text = yaml.safe_dump(content, default_flow_style=False, sort_keys=False, allow_unicode=True)
    OVERRIDE_FILE.write_text(header + yaml_text, encoding="utf-8")


# ---------- Per-agent orchestration ----------

def upper_env_prefix(name: str) -> str:
    return f"AGENT_{name.upper().replace('-', '_')}"


def ensure_user_and_subs(client: BrokerAdmin, agent: dict, env: dict[str, str]) -> tuple[str, bool]:
    """Cria/atualiza user (kind=bot) + subscriptions. Retorna (api_token, rotated)."""
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
        # Backend preservou token mas nao retornou (edge legacy) — usa .env
        api_token = env.get(f"{upper_env_prefix(name)}_TOKEN", "")
        if not api_token:
            die(f"{name}: user ja existe mas .env nao tem token. Delete user e recrie, ou popule {upper_env_prefix(name)}_TOKEN manualmente.")
    # rotated = o .env nao tinha esse token (primeiro reconcile, ou DB recriado
    # com token novo). Containers criados antes precisam ser recreados pra
    # pegar o BROKER_TOKEN novo.
    prev_token = env.get(f"{upper_env_prefix(name)}_TOKEN", "")
    rotated = api_token != prev_token
    for stream in agent.get("streams") or [name]:
        client.upsert_stream(name=stream, description=f"Stream do agente {name}")
        client.subscribe(user_id=user_id, stream=stream)
    log(f"  {name}: user#{user_id} + {len(agent.get('streams') or [name])} stream(s) ok", "ok")
    return api_token, rotated


# Streams que o broker mantem por conta propria (nao vem de agents.yaml).
# Editar quando adicionar novas streams "system-level" configuradas via env.
RESERVED_STREAMS = {"debug"}


def prune_orphans(client: BrokerAdmin, agents: list[dict], env: dict[str, str]) -> None:
    """Soft-delete de streams/bot-users do broker que nao estao mais em
    agents.yaml.

    Estrategia: tenta hard-delete primeiro (DELETE). Se 409 (tem historico),
    cai pra soft-delete (PATCH is_active=false). Soft-delete preserva o
    historico mas remove de listagens da UI e do '## Equipe' do system
    prompt — agente desativado nao reaparece como peer chamavel.

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
        log(f"  falha listando broker: {e}", "err")
        return

    orphan_streams = [s for s in broker_streams if s["name"] not in configured_streams]
    for s in orphan_streams:
        name = s["name"]
        code, body = client.delete_stream(name)
        if code == 200:
            log(f"  stream orfa removida (hard-delete): {name}", "ok")
        elif code == 409:
            # Tem historico de convs. Soft-delete preserva e esconde.
            if s.get("is_active") is False:
                log(f"  stream {name!r} ja inativa", "ok")
            else:
                pcode, pbody = client.set_stream_active(name, False)
                if pcode == 200:
                    log(f"  stream orfa desativada (soft-delete): {name}", "ok")
                else:
                    log(f"  falha desativando stream {name!r}: HTTP {pcode} {pbody[:120]}", "err")
        else:
            log(f"  falha removendo stream {name!r}: HTTP {code} {body[:120]}", "err")

    orphan_bots = [u for u in broker_users
                   if u.get("kind") == "bot"
                   and u.get("agent_name")
                   and u["username"] not in configured_bots
                   and u["username"] != "system-bot"]
    for u in orphan_bots:
        username = u["username"]
        code, body = client.delete_user(username)
        if code == 200:
            log(f"  bot orfao removido (hard-delete): {username}", "ok")
        elif code == 409:
            if u.get("is_active") is False:
                log(f"  user {username!r} ja inativo", "ok")
            else:
                pcode, pbody = client.set_user_active(username, False)
                if pcode == 200:
                    log(f"  bot orfao desativado (soft-delete): {username}", "ok")
                else:
                    log(f"  falha desativando user {username!r}: HTTP {pcode} {pbody[:120]}", "err")
        else:
            log(f"  falha removendo user {username!r}: HTTP {code} {body[:120]}", "err")

    if not orphan_streams and not orphan_bots:
        log("  nada a remover", "ok")


# ---------- Main ----------

def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile agents.yaml -> estado real")
    parser.add_argument("--dry-run", action="store_true", help="so mostra o que faria")
    parser.add_argument("--no-up", action="store_true", help="nao roda docker compose up ao fim")
    parser.add_argument("--no-broker", action="store_true", help="pula users/streams no broker (so FS + override)")
    args = parser.parse_args()

    if not AGENTS_YAML.exists():
        die(f"{AGENTS_YAML} nao existe. Crie a partir de framework/examples/agents.yaml.example.")

    log("Garantindo diretorios da instancia (evita Docker criar como root)", "step")
    # Pastas customizaveis via env (AGENTS_DIR, COMPANY_DIR, BACKUPS_DIR, REPOS_DIR)
    # ou default em instance/. Heartbeats continua em instance/ (ephemeral,
    # sem caso de uso pra mover).
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    COMPANY_DIR.mkdir(parents=True, exist_ok=True)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    (INSTANCE_DIR / "repos").mkdir(parents=True, exist_ok=True)
    (INSTANCE_DIR / "heartbeats").mkdir(parents=True, exist_ok=True)
    # Worktrees precisa ser escrita pelo container (UID node=1000) e lida pelo
    # host (UID do dev). chmod 777 evita collision — mesmo problema que o
    # mount repos/ teve antes (UID 1001 do host vs node do container).
    try:
        WORKTREES_DIR.chmod(0o777)
    except OSError:
        pass

    # System prompts (D-63): pasta + seed do config.yaml. platform.md saiu
    # daqui — virou invariante do framework e mora dentro das imagens
    # (agent/web), via COPY framework/system_prompts. Instancia so seeda o
    # config.yaml (toggles editaveis pelo PWA).
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
                shown = target  # COMPANY_DIR fora do PROJECT_ROOT
            log(f"  seed: {shown} copiado do example", "ok")
    # Migration cleanup: instancias antigas seedaram platform.md aqui. Agora
    # o arquivo vive na imagem; remover o orfao evita confusao (PWA listava
    # o tamanho do arquivo da instance, que estava sendo ignorado pelo runner).
    legacy_platform = sp_dir / "platform.md"
    if legacy_platform.exists():
        legacy_platform.unlink()
        try:
            shown = legacy_platform.relative_to(PROJECT_ROOT)
        except ValueError:
            shown = legacy_platform
        log(f"  removed legacy {shown} (platform.md is now framework-fixed)", "ok")

    log("Parseando agents.yaml", "step")
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
    log(f"{len(agents)} agente(s) na config: {[a['name'] for a in agents]}", "ok")
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
        die("ORCHESTRATOR_TOKEN ausente em env/.env — usado pro reconcile autenticar no broker.")

    client = None
    if not args.no_broker:
        log("Validando conectividade com broker", "step")
        client = BrokerAdmin(broker_url, admin_token)
        try:
            client._get("/api/users/me")
            log(f"broker ok em {broker_url}", "ok")
        except Exception as e:
            die(f"Nao consegui falar com broker em {broker_url}: {e}")

    new_env: dict[str, str] = {}
    rotated_agents: list[str] = []
    for a in agents:
        log(f"\n== Agent: {a['name']} ({a['display_name']}) ==", "step")
        if args.dry_run:
            log("  (dry-run, pulando acoes)", "info")
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
                log(f"  {a['name']}: falha no broker: {e}", "err")
                raise

    if client is not None and not args.dry_run:
        log("\nPrune de streams/users orfaos no broker", "step")
        prune_orphans(client, agents, env)

    if new_env and not args.dry_run:
        log("\nAtualizando .env", "step")
        upsert_env(ENV_FILE, new_env)
        log(f"{len(new_env)} var(s) em .env", "ok")

    log("\nGerando docker-compose.override.yml", "step")
    if args.dry_run:
        log("(dry-run, nao escreve)", "info")
    else:
        write_override(agents, capability_instances)
        log(f"{OVERRIDE_FILE.name} escrito com {len(agents)} service(s)", "ok")

    if args.no_up:
        print("__RECONCILE_NO_UP__")
    elif not args.dry_run:
        print("__RECONCILE_DO_UP__")
        if rotated_agents:
            # Sinaliza agentes cujo token foi recem-criado — wrapper recria
            # os containers pra garantir que peguem o env atualizado.
            print(f"__RECONCILE_ROTATED__ {' '.join(rotated_agents)}")

    log("\nreconcile completo.", "step")
    return 0


if __name__ == "__main__":
    sys.exit(main())
