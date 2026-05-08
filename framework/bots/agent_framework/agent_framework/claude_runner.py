"""Wrapper do claude CLI.

Spawn de subprocess `claude -p <prompt> --output-format stream-json --verbose`,
roda com cwd = diretorio do topic, persiste session_id pra --resume futuro,
e passa `--mcp-config` apontando pro servidor MCP in-process (Fase 1c).
Resposta volta pro broker como mensagem nova (broker nao suporta edicao do
ack "thinking..."); o ack fica como historico.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import yaml

from .log import get_logger
from .mcp.broker import McpBroker
from .memory_store import MemoryStore
from .session_manager import SessionManager
from .internal_client import TopicKey, InternalClient as BrokerClient

log = get_logger(__name__)

# Regex pra remover @-mencoes do proprio bot no texto do usuario
MENTION_RE = re.compile(r"@\*\*[^*]+\*\*\s*")

# Nome do servidor MCP no --mcp-config (claude usa isso como namespace de tools)
MCP_SERVER_NAME = "agent_framework"

# rc que indica kill externo (SIGKILL=137, SIGTERM=143). Retriable.
RETRIABLE_RC = {137, 143}
MAX_ATTEMPTS = 2      # 1 tentativa original + 1 retry
RETRY_BACKOFF_SEC = 5.0

# Pattern emitido pelo Claude CLI quando `--resume X` aponta pra um session_id
# que nao existe em disco. Nosso runner armazena o session_id novo assim que o
# CLI emite `system/init`, mas se a run morre rapido o CLI pode nao ter tempo
# de persistir o .jsonl do fork — ficamos com um id fantasma no DB que explode
# na proxima tentativa. Detectar → limpar session_id + retry sem `--resume`
# (D-70).
GHOST_SESSION_RE = re.compile(r"No conversation found with session ID:\s*([a-f0-9-]+)")

# Handoff body postado pelo reactor (orchestrator/reactor.py:_handoff_body).
# Captura task_slug + step assumido. Usado em handle() pra detectar handoffs
# stale (task ja em status terminal ou fase ja concluida) — quando o reactor
# posta um handoff durante uma run que ja absorveu a fase via --resume
# continuity, a mensagem fica na fila do dispatcher e seria processada num
# spawn extra, gerando resposta "atrasado, ja feito" (puro desperdicio).
# Sintoma observado: ops/2026-05-05 fix-task-16 (2 cascade runs pos-`done`).
HANDOFF_BODY_RE = re.compile(
    r"^➡️ \*\*Handoff from.*?\*\*Task:\*\* `([a-z0-9][a-z0-9-]*)` — você assume a fase \*\*([a-z0-9_-]+)\*\*",
    re.S,
)

# Diretorio onde o Claude CLI persiste o jsonl de cada session. Layout:
#   ~/.claude/projects/<cwd-encoded>/<session_id>.jsonl
# O subdir por cwd e derivado do CLI, entao vasculhamos todos.
_CLAUDE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"

# Buffer por linha do stream-json do Claude CLI. Default do asyncio e 64 KiB,
# estourado toda vez que um tool_use_result grande (Read de arquivo de
# ~40-50 KB com line-numbers + JSON-escape, Bash com output longo, etc.) cai
# numa unica linha. Subimos pra 16 MiB; linhas maiores sao tratadas como
# oversized e o turn retenta com prompt de recovery orientando leitura parcial.
STREAM_STDOUT_LIMIT_BYTES = 16 * 1024 * 1024

# Prompt usado na retentativa apos uma linha oversized. Vai como novo turn via
# `--resume`, pedindo pro Claude refazer o passo em fatias.
OVERSIZED_RECOVERY_PROMPT = (
    "The previous turn was interrupted by the framework: a single line of "
    "Claude's stream-json (typically the result of a tool_use like "
    "Read/Bash/Grep) exceeded the internal 16 MiB limit. The response was "
    "discarded.\n\n"
    "Please redo the previous step using **partial reads**, so you don't "
    "blow the buffer again:\n"
    "- `Read`: pass `offset` + `limit` (paginate in chunks of ~200-500 lines).\n"
    "- `Bash`: prefer `head -n`, `tail -n`, `sed -n 'A,Bp'`, `grep -n ... | "
    "head`, `wc -l`. Avoid `cat` on large files.\n"
    "- `Grep`: use `-n` with a small `-C` instead of full dumps.\n\n"
    "Continue from where you stopped and deliver the same final answer."
)

# D-63: secoes de instancia (CONTEXT, philosophy, agent CLAUDE.md, bloco Equipe)
# editaveis via PWA. platform.md e invariante do framework — vive em
# /app/system_prompts/platform.md (COPY no Dockerfile do agent), nao e
# editavel pela instancia. claude_runner re-le tudo a cada invocacao —
# edicoes valem na proxima task, sem restart.
_SYSTEM_PROMPTS_DIR = Path("/workspace/company/system_prompts")
_PLATFORM_PROMPT_PATH = Path("/app/system_prompts/platform.md")
_SYSTEM_PROMPTS_CONFIG_PATH = _SYSTEM_PROMPTS_DIR / "config.yaml"
_WORKFLOWS_YAML_PATH = Path("/workspace/company/workflows.yaml")

# Defaults aplicados se config.yaml ausente ou chave faltando — todas as secoes
# ON. Reconcile copia config.yaml.example no primeiro setup, entao em prod
# o arquivo sempre existe; defaults sao ultimo fallback.
_SYSTEM_PROMPT_TOGGLE_DEFAULTS: dict[str, bool] = {
    "include_platform_prompt": True,
    "include_company_context": True,
    "include_company_philosophy": True,
    "include_agent_claude_md": True,
    "include_team_block": True,
    "include_invocation_context": True,
    "include_task_state": True,
    "include_step_instructions": True,
}


def _load_system_prompt_toggles() -> dict[str, bool]:
    """Le config.yaml dos system prompts e devolve dict de toggles.

    Falhas de IO/parse caem no default (todas ON) com warning — preferimos
    nao quebrar runs por config corrompida. O usuario edita pelo PWA ou
    direto no arquivo; falha na leitura = comportamento como pre-D-63.
    """
    toggles = dict(_SYSTEM_PROMPT_TOGGLE_DEFAULTS)
    try:
        raw = _SYSTEM_PROMPTS_CONFIG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return toggles
    except Exception as e:
        log.warning("runner.system_prompt_config_read_failed", err=str(e))
        return toggles
    try:
        data = yaml.safe_load(raw) or {}
    except Exception as e:
        log.warning("runner.system_prompt_config_parse_failed", err=str(e))
        return toggles
    if not isinstance(data, dict):
        log.warning("runner.system_prompt_config_invalid", got=type(data).__name__)
        return toggles
    for key in toggles:
        v = data.get(key)
        if isinstance(v, bool):
            toggles[key] = v
    return toggles


@dataclass
class RunOutcome:
    ok: bool
    rc: int | None
    result_text: str = ""
    session_id: str | None = None
    error_subtype: str | None = None
    stderr: str = ""
    total_cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    usage: dict | None = None
    tool_uses: list[str] = field(default_factory=list)
    # Quantas linhas do stream-json foram descartadas por exceder
    # STREAM_STDOUT_LIMIT_BYTES. >0 sinaliza que o parser pulou conteudo;
    # se o `result` final nao chegou, o turn e retriable com recovery prompt.
    oversized_lines_skipped: int = 0
    # Claude CLI reclamou que o `--resume <sid>` aponta pra session inexistente
    # (ghost session id fruto de fork que nao foi gravado em disco). Loop de
    # tentativas usa isso pra limpar o DB e retentar sem `--resume` sem gastar
    # um attempt normal (D-70).
    ghost_session: bool = False

    @property
    def retriable(self) -> bool:
        # Kill externo: container foi restartado ou OOM
        if self.rc in RETRIABLE_RC:
            return True
        # Falha sem nenhuma resposta parcial — provavelmente interrupcao
        if (self.rc or 0) != 0 and not self.result_text:
            return True
        # Parser pulou linha(s) gigantes e nao conseguiu montar resposta final:
        # retentar com recovery prompt orientando leitura parcial.
        if self.oversized_lines_skipped and not self.result_text:
            return True
        return False


_COMPANY_CONTEXT_PATH = Path("/workspace/company/CONTEXT.md")
_COMPANY_PHILOSOPHY_PATH = Path("/workspace/company/philosophy.md")
_COMPANY_FILE_CAP_BYTES = 8 * 1024  # cap injetado no system prompt


def _read_capped(path: Path, cap: int = _COMPANY_FILE_CAP_BYTES) -> str:
    """Le arquivo + corta se passar do cap. Vazio se nao existe."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    enc = text.encode("utf-8")
    if len(enc) <= cap:
        return text
    return enc[:cap].decode("utf-8", errors="ignore") + "\n\n_(truncated)_"


def _auth_headers() -> dict[str, str]:
    """Bearer com BROKER_TOKEN pros endpoints autenticados do web (telemetry/
    live-event). Pos-D-95 o web nao tem mais dev bypass, entao posts sem
    header retornam 401 e os tool_uses/thinkings somem do PWA."""
    tok = os.environ.get("BROKER_TOKEN")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _session_jsonl_exists(session_id: str) -> bool:
    """True se o Claude CLI ja persistiu `<session_id>.jsonl` em disco.

    O CLI emite o session_id novo no primeiro evento `system/init`, mas so
    grava o jsonl conforme o turn avanca. Se a run morre cedo (ex: erro de
    init, SIGKILL rapido, resume apontando pra ghost), o id existe no stream
    mas nunca toca o filesystem. Usar isso pra decidir se vale persistir no
    DB evita ghost loops onde o proximo `--resume` explode com `No conversation
    found with session ID`.
    """
    if not session_id:
        return False
    root = _CLAUDE_PROJECTS_ROOT
    if not root.is_dir():
        return False
    # Projeto por cwd — percorremos todos. Diretorio e enxuto (1 por topic),
    # stat e barato; nao vale cache.
    target = f"{session_id}.jsonl"
    for proj_dir in root.iterdir():
        if not proj_dir.is_dir():
            continue
        if (proj_dir / target).is_file():
            return True
    return False


class ClaudeRunner:
    def __init__(
        self,
        session_mgr: SessionManager,
        broker_client: BrokerClient,
        broker: McpBroker | None = None,
        mcp_url_for: Any = None,  # callable: slug -> url
        allowed_tools: list[str] | None = None,
        telemetry_url: str | None = None,
        agent_name: str | None = None,
        memory: MemoryStore | None = None,
        memory_auto_inject_limit: int = 0,
        model: str | None = None,
        effort: str | None = None,
        db_pool: Any = None,  # asyncpg.Pool | None — usado pra montar bloco "## Equipe"
    ):
        self.session_mgr = session_mgr
        self.broker_client = broker_client
        self.broker = broker
        self.mcp_url_for = mcp_url_for
        # Permite todas as tools do MCP do framework por padrao
        self.allowed_tools = allowed_tools or [f"mcp__{MCP_SERVER_NAME}__ask_human"]
        self.telemetry_url = telemetry_url
        self.agent_name = agent_name
        self.memory = memory
        self.memory_auto_inject_limit = memory_auto_inject_limit
        self.model = model
        self.effort = effort
        self.db_pool = db_pool
        # D-71: dispatcher e injetado via setter apos construcao (ordem em
        # main.py: runner antes de dispatcher). Usado em `_run_claude_once`
        # pra registrar proc ativo e permitir cancel por SIGTERM. Opcional
        # — None mantem comportamento pre-D-71.
        self._dispatcher: Any = None
        # Contador monotônico por (stream, topic) pra seq_num de live_events
        # (migration 018). claude_runner emite thinking/tool_use via
        # asyncio.create_task (fire-and-forget); sem seq gerado aqui, a
        # ordem de INSERT no banco nao respeita a ordem do stream do SDK
        # e o frontend desempata errado. Sem lock: asyncio e single-thread,
        # o incremento e atomico contanto que aconteca antes do primeiro
        # await dentro de _emit_live.
        self._live_seq_by_topic: dict[str, int] = {}

    def bind_dispatcher(self, dispatcher: Any) -> None:
        """Injeta referencia do Dispatcher pos-construcao. D-71.

        Usado pelo runner pra chamar `handler_register_proc` /
        `handler_unregister_proc` em volta do `create_subprocess_exec`.
        """
        self._dispatcher = dispatcher

    async def _team_block(self, ctx: dict[str, Any] | None = None) -> str:
        """Monta bloco '## Equipe' filtrado: lista os outros agentes que
        este agente PODE chamar via ask_agent. Vazio se db_pool ausente,
        agente nao tem colegas elegiveis, ou esta em conv filha (filha nao
        pode chamar ninguem — listar peers seria desinformacao)."""
        if ctx and ctx.get("is_child"):
            return ""
        if self.db_pool is None or not self.agent_name:
            return ""
        try:
            async with self.db_pool.acquire() as conn:
                policy = await conn.fetchrow(
                    "SELECT can_ask FROM messaging.agent_policies WHERE agent = $1",
                    self.agent_name,
                )
                rows = await conn.fetch(
                    """SELECT u.agent_name AS name, u.full_name AS display_name
                         FROM messaging.users u
                        WHERE u.kind = 'bot'
                          AND u.agent_name IS NOT NULL
                          AND u.agent_name <> $1
                          AND u.is_active = true
                        ORDER BY u.agent_name""",
                    self.agent_name,
                )
        except Exception:
            log.exception("runner.team_block_failed", agent=self.agent_name)
            return ""
        whitelist = policy["can_ask"] if policy and policy["can_ask"] is not None else None
        peers = []
        for r in rows:
            name = r["name"]
            if whitelist is not None and name not in whitelist:
                continue
            peers.append(f"- **{name}** ({r['display_name']})")
        if not peers:
            return (
                "\n\n## Team\n\n"
                "_No other agents available for `ask_agent` right now. "
                "Use `ask_human` if you need to delegate._"
            )
        return (
            "\n\n## Team (agents you can call via `ask_agent` or `ask_agents_many`)\n\n"
            + "\n".join(peers)
        )

    async def _resolve_invocation_context(
        self, conv_id: int | None
    ) -> dict[str, Any]:
        """Detecta se a conv corrente e filha de outra (e de quem). Fonte de
        verdade: `messaging.conversations.parent_conv_id` — mesma usada pelo
        MCP gate (server.py) que rejeita ask_human/ask_agent em filha com 409.

        Retorna `{"is_child": bool, "parent_label": str | None}`. Em ausencia
        de db_pool / conv_id / linha — retorna `is_child=False` (default raiz),
        que eh o comportamento mais permissivo e bate com runs sem broker
        (testes / mock).
        """
        default: dict[str, Any] = {"is_child": False, "parent_label": None}
        if self.db_pool is None or conv_id is None:
            return default
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT pc.id AS parent_id,
                              ps.name AS parent_stream,
                              pu.agent_name AS parent_agent_name
                         FROM messaging.conversations c
                         LEFT JOIN messaging.conversations pc ON pc.id = c.parent_conv_id
                         LEFT JOIN messaging.streams ps ON ps.id = pc.stream_id
                         LEFT JOIN messaging.users pu
                                ON pu.agent_name = ps.name AND pu.kind = 'bot'
                        WHERE c.id = $1""",
                    conv_id,
                )
        except Exception:
            log.exception("runner.invocation_context_failed", agent=self.agent_name)
            return default
        if row is None or row["parent_id"] is None:
            return default
        return {
            "is_child": True,
            "parent_label": row["parent_agent_name"] or row["parent_stream"],
        }

    @staticmethod
    def _invocation_context_block(ctx: dict[str, Any]) -> str:
        """Bloco '## Modo de invocacao' — injetado **so em filha**. Em raiz,
        o bloco "## Equipe" abaixo ja sinaliza implicitamente que o agente
        pode chamar peers; nao precisa de bloco "voce e raiz" redundante.

        Em filha, bloco curto: nome do pai + regra de reply-as-gateway.
        Reply auto sempre funciona; tool de mensageria em filha falha 409.
        """
        if not ctx.get("is_child"):
            return ""
        parent_label = ctx.get("parent_label") or "unknown agent"
        return (
            "\n\n## Invocation mode\n\n"
            f"This conv is a **child** of **`{parent_label}`** (`ask_agent` or "
            "phase handoff). `ask_human`/`ask_agent`/`ask_agents_many` "
            "are blocked here (MCP gate 409, root->child 1-level hierarchy).\n\n"
            f"To request external input (human decision, specialist, "
            f"opinion outside scope): **describe the request at the end of "
            f"the response in a format ready for `{parent_label}` to forward "
            f"verbatim** and end the turn. The parent receives the auto-reply "
            "and routes it.\n\n"
            "Any instruction like \"call `ask_human` X\" / \"call "
            "`ask_agent <Y>` X\" must be read as \"describe in the reply: "
            "needs X\".\n"
        )

    async def _task_state_block(self, topic_key: TopicKey) -> str:
        """Bloco '## Estado da task' — quando o topic e `task-<slug>`, lista
        estado canonico (workflow, current_step, complexity, baseline,
        worktrees, phases_done) ja resolvido pelo framework. Evita o ritual
        de `get_task_state` no inicio de cada fase.

        Nao injeta se topic nao e task-* (modo analise / chat livre).
        """
        if self.db_pool is None:
            return ""
        topic = topic_key.topic
        if not topic.startswith("task-"):
            return ""
        slug = topic[5:]
        if not slug:
            return ""
        try:
            async with self.db_pool.acquire() as conn:
                task = await conn.fetchrow(
                    """SELECT id, slug, title, workflow, status, current_step,
                              current_agent, complexity, impact, difficulty,
                              origin, origin_stream, origin_topic, blocked_reason,
                              metadata_extra
                         FROM tasks.tasks
                        WHERE slug = $1 AND archived_at IS NULL""",
                    slug,
                )
                if task is None:
                    return ""
                phases = await conn.fetch(
                    """SELECT step, agent, artifact, completed_at
                         FROM tasks.phases
                        WHERE task_id = $1 AND completed_at IS NOT NULL
                        ORDER BY completed_at""",
                    task["id"],
                )
                worktrees = await conn.fetch(
                    """SELECT repo, path, branch
                         FROM tasks.worktrees
                        WHERE task_id = $1
                        ORDER BY repo""",
                    task["id"],
                )
        except Exception:
            log.exception("runner.task_state_block_failed", agent=self.agent_name, slug=slug)
            return ""
        meta = task["metadata_extra"]
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        elif not isinstance(meta, dict):
            meta = {}
        baseline = (meta or {}).get("baseline") or {}
        lines: list[str] = ["\n\n## Task state\n"]
        lines.append(f"- **Slug:** `{task['slug']}`")
        lines.append(f"- **Title:** {task['title']}")
        if task["workflow"]:
            lines.append(f"- **Workflow:** `{task['workflow']}`")
        lines.append(f"- **Status:** `{task['status']}`")
        if task["current_step"]:
            agent_str = f" (agent: `{task['current_agent']}`)" if task["current_agent"] else ""
            lines.append(f"- **Current step:** `{task['current_step']}`{agent_str}")
        if task["complexity"]:
            lines.append(f"- **Complexity:** `{task['complexity']}`")
        for k, label in (("impact", "Impact"), ("difficulty", "Difficulty")):
            if task[k]:
                lines.append(f"- **{label}:** `{task[k]}`")
        if task["blocked_reason"]:
            lines.append(f"- **Blocked:** {task['blocked_reason']}")
        if phases:
            done_str = " -> ".join(
                f"`{p['step']}`" + (f" ({p['artifact']})" if p["artifact"] else "")
                for p in phases
            )
            lines.append(f"- **Phases done:** {done_str}")
        else:
            lines.append("- **Phases done:** _(none — task just started)_")
        if baseline:
            base_str = ", ".join(f"`{repo}@{sha[:8]}`" for repo, sha in sorted(baseline.items()))
            lines.append(f"- **Registered baselines:** {base_str}")
        else:
            lines.append("- **Registered baselines:** _(none)_")
        if worktrees:
            wt_lines = [
                f"  - `{w['repo']}` at `{w['path']}` (branch `{w['branch']}`)"
                for w in worktrees
            ]
            lines.append("- **Active worktrees:**")
            lines.extend(wt_lines)
        else:
            lines.append("- **Active worktrees:** _(none)_")
        if task["origin_stream"] and task["origin_topic"]:
            lines.append(
                f"- **Origin:** stream `{task['origin_stream']}`, topic "
                f"`{task['origin_topic']}` (terminal notification comes back here)"
            )
        lines.append("")
        lines.append(
            "_State read from the DB at spawn time (snapshot). For fresh data "
            "after a mid-turn transition, call `get_task_state` via MCP._"
        )
        return "\n".join(lines)

    async def _load_current_step_doc(
        self, topic_key: TopicKey
    ) -> tuple[str, str, dict] | None:
        """Resolve `(workflow_name, step_name, step_dict)` pra task em
        andamento no topic.

        Retorna None quando: db_pool ausente, topic nao-task, slug vazio,
        task arquivada/inexistente, workflow sem step corrente, workflows.yaml
        ausente/parseavel, ou step nao declarado no yaml.

        Compartilhado entre `_step_instructions_block` (bloco no system prompt)
        e `_step_overrides` (config runtime). Cada chamada le yaml do disco —
        edicao via PWA aplica no proximo spawn sem rebuild.
        """
        if self.db_pool is None:
            return None
        topic = topic_key.topic
        if not topic.startswith("task-"):
            return None
        slug = topic[5:]
        if not slug:
            return None
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT workflow, current_step
                         FROM tasks.tasks
                        WHERE slug = $1 AND archived_at IS NULL""",
                    slug,
                )
        except Exception:
            log.exception("runner.current_step_query_failed", slug=slug)
            return None
        if row is None:
            return None
        wf_name = row["workflow"]
        step_name = row["current_step"]
        if not wf_name or not step_name:
            return None
        try:
            wf_text = _WORKFLOWS_YAML_PATH.read_text(encoding="utf-8")
            wf_doc = yaml.safe_load(wf_text) or {}
        except FileNotFoundError:
            return None
        except Exception:
            log.exception("runner.workflows_yaml_parse_failed")
            return None
        wf = (wf_doc.get("workflows") or {}).get(wf_name) or {}
        step = (wf.get("steps") or {}).get(step_name) or {}
        if not isinstance(step, dict):
            return None
        return wf_name, step_name, step

    async def _step_instructions_block(self, topic_key: TopicKey) -> str:
        """Bloco '## Instrucoes da fase atual' — le `workflows.yaml` e injeta
        as `instructions` (markdown) declaradas pelo step corrente da task.

        Sem task em andamento, sem step ou sem `instructions` declarado,
        retorna "" (silencio — agentes sem step ativo sao analise/chat livre).
        """
        doc = await self._load_current_step_doc(topic_key)
        if doc is None:
            return ""
        wf_name, step_name, step = doc
        instructions = step.get("instructions")
        if not isinstance(instructions, str) or not instructions.strip():
            return ""
        artifact = step.get("artifact")
        next_steps = step.get("next") or []
        header_lines = [
            "\n\n## Current phase instructions",
            "",
            f"_Workflow `{wf_name}` -> step `{step_name}`._",
        ]
        if artifact:
            header_lines.append(f"_Expected artifact: `{artifact}`._")
        if next_steps:
            nxt = " | ".join(f"`{n}`" for n in next_steps)
            header_lines.append(f"_Valid transitions: {nxt}._")
        header_lines.append("")
        return "\n".join(header_lines) + instructions.rstrip() + "\n"

    async def _step_overrides(self, topic_key: TopicKey) -> dict:
        """Retorna o sub-dict `overrides` declarado no step corrente da task
        em `workflows.yaml`. Vazio se nao ha task / step / overrides.

        Filtra apenas chaves whitelisted (`model`, `effort`, `memory`) e
        valida tipos basicos. Valores invalidos no YAML (edicao manual fora
        da PWA) sao ignorados com log warn — nunca crasha o spawn.
        """
        doc = await self._load_current_step_doc(topic_key)
        if doc is None:
            return {}
        _, step_name, step = doc
        raw = step.get("overrides")
        if not isinstance(raw, dict):
            return {}
        out: dict = {}
        model = raw.get("model")
        if isinstance(model, str) and model.strip():
            out["model"] = model.strip()
        elif "model" in raw:
            log.warning(
                "runner.step_overrides_invalid",
                step=step_name, field="model", value=model,
            )
        effort = raw.get("effort")
        if isinstance(effort, str) and effort.strip():
            out["effort"] = effort.strip()
        elif "effort" in raw:
            log.warning(
                "runner.step_overrides_invalid",
                step=step_name, field="effort", value=effort,
            )
        mem = raw.get("memory")
        if isinstance(mem, dict):
            mem_out: dict = {}
            if "enabled" in mem:
                if isinstance(mem["enabled"], bool):
                    mem_out["enabled"] = mem["enabled"]
                else:
                    log.warning(
                        "runner.step_overrides_invalid",
                        step=step_name, field="memory.enabled", value=mem["enabled"],
                    )
            if "auto_inject_limit" in mem:
                lim = mem["auto_inject_limit"]
                if isinstance(lim, int) and not isinstance(lim, bool) and lim >= 0:
                    mem_out["auto_inject_limit"] = lim
                else:
                    log.warning(
                        "runner.step_overrides_invalid",
                        step=step_name, field="memory.auto_inject_limit", value=lim,
                    )
            if mem_out:
                out["memory"] = mem_out
        elif "memory" in raw:
            log.warning(
                "runner.step_overrides_invalid",
                step=step_name, field="memory", value=mem,
            )
        return out

    async def _build_system_prompt(
        self,
        conv_id: int | None = None,
        topic_key: TopicKey | None = None,
    ) -> str:
        """Concatena as secoes habilitadas em system_prompts/config.yaml.

        Secoes estaticas (toggleable):
          - platform.md (regras invariantes do framework)
          - CLAUDE.md do agente (identidade do papel)
          - company/CONTEXT.md (catalogo da instancia)
          - company/philosophy.md (opcional)

        Secoes dinamicas (toggleable, geradas a cada spawn a partir do DB / FS):
          - "## Invocation mode" — raiz vs filha + nome do pai (se filha)
          - "## Task state" — quando topic = task-*, snapshot do estado
          - "## Current phase instructions" — quando step da task tem `instructions`
            declaradas em workflows.yaml
          - "## Team" — peers que o agente pode chamar via ask_agent

        D-63: nada hardcoded — tudo le do FS / DB a cada invocacao. Edicao
        pelo PWA (ou direto nos arquivos) vale na proxima task, sem restart.
        D-61: CLAUDE.md do agente entra aqui (nao mais via cwd-walker), tornando
        a identidade cwd-independente.
        """
        toggles = _load_system_prompt_toggles()
        parts: list[str] = []

        # Resolve raiz vs filha uma vez — usado tanto pelo bloco de invocacao
        # quanto pelo team_block (que omite peers em filha pra nao dar
        # desinformacao: filha nao pode chamar ninguem).
        invocation_ctx = await self._resolve_invocation_context(conv_id)

        if toggles["include_platform_prompt"]:
            try:
                platform_text = _PLATFORM_PROMPT_PATH.read_text(encoding="utf-8")
            except FileNotFoundError:
                raise RuntimeError(
                    f"system_prompt: {_PLATFORM_PROMPT_PATH} ausente na "
                    "imagem do agent. Sintoma de build incompleto — rebuilde "
                    "a imagem `agent` (a COPY de framework/system_prompts "
                    "deveria ter trazido o arquivo). Toggle off em "
                    "system_prompts/config.yaml so como ultimo recurso."
                )
            parts.append(platform_text)

        if toggles.get("include_invocation_context", True):
            invocation = self._invocation_context_block(invocation_ctx)
            if invocation:
                parts.append(invocation)

        if topic_key is not None:
            if toggles.get("include_task_state", True):
                state = await self._task_state_block(topic_key)
                if state:
                    parts.append(state)
            if toggles.get("include_step_instructions", True):
                step = await self._step_instructions_block(topic_key)
                if step:
                    parts.append(step)

        if toggles["include_agent_claude_md"] and self.agent_name:
            agent_md = Path(f"/app/agents/{self.agent_name}/CLAUDE.md")
            agent_md_text = _read_capped(agent_md)
            if agent_md_text.strip():
                parts.append("\n\n# Agent instructions\n\n" + agent_md_text)

        if toggles["include_company_context"]:
            company_ctx = _read_capped(_COMPANY_CONTEXT_PATH)
            if company_ctx.strip():
                parts.append("\n\n# Company context\n\n" + company_ctx)

        if toggles["include_company_philosophy"]:
            phi = _read_capped(_COMPANY_PHILOSOPHY_PATH, cap=4 * 1024)
            # `_(optional` / `_(opcional` are seed markers in the example file
            # that mean "instance hasn't filled this in yet" — skip injection.
            if phi.strip() and "_(optional" not in phi and "_(opcional" not in phi:
                parts.append("\n\n# Operational philosophy\n\n" + phi)

        if toggles["include_team_block"]:
            team = await self._team_block(invocation_ctx)
            if team:
                parts.append(team)

        return "".join(parts)

    async def _resolve_spawn_cwd(self, default_cwd: Path, topic_key: TopicKey) -> Path:
        """Retorna o cwd pra subir o `claude -p`.

        Worktree e usada SOMENTE quando este run esta executando uma fase de
        workflow da task `<slug>`, identificado por `topic_key.topic` no formato
        `task-<slug>`. Topics ad-hoc (chat livre, `__ask-from-*`, `__child-*`,
        rotulos com data) ficam no session_dir mesmo se o agente tiver outras
        tasks in_progress em paralelo (D-97).

        Regra: se `topic = task-<slug>` E existe worktree registrada pra essa
        `<slug>` cujo `current_agent` eh este agente E `status='in_progress'`
        E tem path em disco, usa o path. Caso contrario, default_cwd.

        Antes (D-61), o lookup era so por `current_agent`+`in_progress`, sem
        join no slug do topic. Agente com 1 task in_progress numa conv X via
        cwd da task vazar pra outra conv Y simultanea — `--resume` falhava
        em ghost porque o sid de Y vivia no project dir do cwd antigo. Restringir
        ao topic da propria task elimina o cross-talk.

        Motivo de usar worktree: claude_code carrega `.claude/{agents,commands}/`
        + `CLAUDE.md` do repo nativamente quando cwd=worktree. Identidade do
        agente vai por canais cwd-independentes (system prompt, `--add-dir`).
        """
        if not self.agent_name or self.db_pool is None:
            return default_cwd
        # Topic neutro: so reconhecemos o prefixo `task-` (gerado pelo reactor
        # em `target_topic = task-<slug>`). Sem isso, nao da pra mapear topic
        # → task slug sem heuristicas.
        topic = topic_key.topic
        if not topic.startswith("task-"):
            return default_cwd
        task_slug = topic[len("task-"):]
        if not task_slug:
            return default_cwd
        try:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT w.path
                    FROM tasks.worktrees w
                    JOIN tasks.tasks t ON t.id = w.task_id
                    WHERE t.slug = $1
                      AND t.current_agent = $2
                      AND t.status = 'in_progress'
                      AND w.path IS NOT NULL
                    """,
                    task_slug, self.agent_name,
                )
        except Exception as e:
            log.warning(
                "runner.spawn_cwd_lookup_failed",
                err=str(e), agent=self.agent_name, task_slug=task_slug,
            )
            return default_cwd
        if len(rows) != 1:
            # 0: task sem worktree (ex: pre-execucao) ou agente nao eh
            #    o current_agent dessa task. >1: ambiguo (multi-repo) — agente
            #    navega via paths absolutos com --add-dir.
            return default_cwd
        path = Path(rows[0]["path"])
        if not path.is_dir():
            log.warning(
                "runner.spawn_cwd_missing_path",
                agent=self.agent_name,
                task_slug=task_slug,
                path=str(path),
            )
            return default_cwd
        return path

    async def _inject_memory(self, prompt: str, overrides: dict | None = None) -> str:
        """Prepend os N fatos mais relevantes da memoria no prompt.
        Sem-op se memory desligada (no agente ou via override do step) ou limit<=0.

        Overrides aceitos (vindo de `workflows.yaml.steps.<step>.overrides.memory`):
          - `enabled=False` → desliga inject neste spawn (mesmo com agente
            tendo MemoryStore configurado).
          - `auto_inject_limit=N` → usa N em vez do default do agente.
        """
        mem_ov = (overrides or {}).get("memory") or {}
        if mem_ov.get("enabled") is False:
            log.info(
                "runner.memory_override_applied",
                agent=self.agent_name, field="enabled", to=False,
            )
            return prompt
        limit = mem_ov.get("auto_inject_limit", self.memory_auto_inject_limit)
        if mem_ov.get("auto_inject_limit") is not None and limit != self.memory_auto_inject_limit:
            log.info(
                "runner.memory_override_applied",
                agent=self.agent_name, field="auto_inject_limit",
                from_=self.memory_auto_inject_limit, to=limit,
            )
        if self.memory is None or limit <= 0:
            return prompt
        try:
            facts = await self.memory.recall(prompt, limit=limit)
        except Exception:
            log.exception("runner.memory_recall_failed")
            return prompt
        if not facts:
            return prompt
        lines = ["## Memory (facts you've already recorded about this context)", ""]
        for f in facts:
            tag_str = f" [{', '.join(f['tags'])}]" if f.get("tags") else ""
            lines.append(f"- **{f['key']}**{tag_str}: {f['value']}")
        lines.extend([
            "",
            "_(The facts above are historical context only — pulled from your persistent memory. "
            "Use `memory_recall` to search for more, `memory_save` to add new facts relevant to future sessions.)_",
            "",
            "---",
            "",
            prompt,
        ])
        log.info("runner.memory_injected", count=len(facts), agent=self.agent_name)
        return "\n".join(lines)

    async def _emit_telemetry(self, payload: dict) -> None:
        """Fire-and-forget pro web (com timeout curto). Falha silenciosa."""
        if not self.telemetry_url or not self.agent_name:
            return
        payload = {**payload, "agent": self.agent_name}
        try:
            timeout = aiohttp.ClientTimeout(total=3)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(self.telemetry_url, json=payload, headers=_auth_headers()) as r:
                    if r.status >= 400:
                        log.debug("runner.telemetry_bad_status", status=r.status)
        except Exception as e:
            log.debug("runner.telemetry_failed", error=str(e))

    async def _emit_live(self, topic_key: TopicKey, kind: str, summary: str = "", data: dict | None = None) -> None:
        """Fire-and-forget pro endpoint live trace (mesma URL base do telemetry,
        substitui '/event' -> '/live-event'). Skip silente em falha.

        `seq_num` (migration 018) e gerado aqui, antes do fire-and-forget,
        pra garantir ordem determinista mesmo quando os POSTs chegam ao
        banco fora de ordem. Contador por-conversa (stream+topic), incrementa
        sob lock pra cobrir emits concorrentes.
        """
        if not self.telemetry_url or not self.agent_name:
            return
        live_url = self.telemetry_url.rsplit("/event", 1)[0] + "/live-event"
        slug = topic_key.slug()
        seq = self._live_seq_by_topic.get(slug, 0) + 1
        self._live_seq_by_topic[slug] = seq
        # Cap alto (8KB) pra comportar thinking multi-paragrafo; UI renderiza
        # cheio. Se estourar, o backend ainda tem um segundo cap seguro.
        payload = {
            "stream": topic_key.stream,
            "topic": topic_key.topic,
            "agent": self.agent_name,
            "kind": kind,
            "summary": summary[:8000] if summary else "",
            "data": data or {},
            "seq_num": seq,
        }
        try:
            timeout = aiohttp.ClientTimeout(total=2)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(live_url, json=payload, headers=_auth_headers()) as r:
                    if r.status >= 400:
                        log.debug("runner.live_bad_status", status=r.status, kind=kind)
        except Exception as e:
            log.debug("runner.live_failed", error=str(e))

    async def _run_claude_once(
        self,
        *,
        prompt: str,
        workdir: Path,
        mcp_config_path: Path,
        resume_sid: str | None,
        resume_sid_cwd: str | None,
        topic_key: TopicKey,
        conv_id: int | None = None,
        overrides: dict | None = None,
    ) -> RunOutcome:
        """Uma invocacao de claude -p. Retorna outcome (sem postar nada).

        `resume_sid_cwd` e o cwd onde `resume_sid` foi gravado (D-97). Se difere
        do `spawn_cwd` resolvido agora, pulamos `--resume` proativamente — o CLI
        guarda o `<sid>.jsonl` em `~/.claude/projects/<encoded-cwd>/`, retomar
        de outro cwd produz ghost. None significa "id legacy sem cwd registrado"
        (back-compat com sids gravados antes da migration 024) — aceita.
        """
        import os
        # Monta o system prompt UMA vez antes do branch mock/real — assim a
        # telemetria de assembly aparece em ambos os caminhos. Edits no prompt
        # (platform.md, workflows.yaml, CLAUDE.md) sao lidos do FS aqui.
        sys_prompt = await self._build_system_prompt(conv_id=conv_id, topic_key=topic_key)
        log.info(
            "runner.sys_prompt_assembled",
            agent=self.agent_name,
            topic=topic_key.slug(),
            chars=len(sys_prompt),
            has_invocation_block="\n## Modo de invocacao\n" in sys_prompt,
            has_task_state_block="\n## Estado da task\n" in sys_prompt,
            has_step_instructions_block="\n## Instrucoes da fase atual\n" in sys_prompt,
            has_team_block="\n## Equipe" in sys_prompt,
        )
        # Mock mode pra testes e2e: nao invoca Claude, devolve resposta scriptada.
        # Aceita "0"/"false"/"no"/"off"/vazio como desligado (sem isso, `CLAUDE_MOCK=0`
        # ativaria mock porque `"0"` eh truthy em Python).
        _mock_flag = os.environ.get("CLAUDE_MOCK", "").strip().lower()
        if _mock_flag not in ("", "0", "false", "no", "off"):
            mock_reply = os.environ.get("CLAUDE_MOCK_REPLY",
                f"[mock reply] agente {self.agent_name} recebeu prompt ({len(prompt)} chars)")
            log.info("runner.mock_reply", topic=topic_key.slug(), agent=self.agent_name)
            # Emite live events sinteticos pro PWA ver atividade tambem em mock.
            await self._emit_live(
                topic_key, "run_start",
                summary=f"mock resume={bool(resume_sid)}",
            )
            await self._emit_live(
                topic_key, "run_end",
                summary="mock turns=1 cost=$0.0000 dur=1ms",
                data={"mock": True},
            )
            return RunOutcome(
                ok=True, rc=0, result_text=mock_reply, session_id=resume_sid,
                total_cost_usd=0.0, duration_ms=1, num_turns=1,
                usage={"input_tokens": len(prompt), "output_tokens": len(mock_reply)},
                tool_uses=[],
            )
        # D-90: refresh sincrono das credenciais OAuth do host antes de cada
        # spawn. /tmp/host-claude/ e dir bind do ~/.claude/ do host (inode-safe
        # via path resolution); copiamos pro volume do agente pra ele continuar
        # gravando refresh tokens em rw localmente. Custo: 1 cp de ~500 bytes.
        # Sem isso, container fica preso no token capturado no entrypoint, e
        # quando o host rotaciona (write-then-rename = novo inode) o file bind
        # antigo apontava pro inode morto -> 401 silencioso (D-55).
        try:
            host_creds = Path("/tmp/host-claude/.credentials.json")
            if host_creds.is_file():
                shutil.copyfile(host_creds, "/home/node/.claude/.credentials.json")
        except Exception as e:
            log.debug("runner.creds_sync_failed", error=str(e))

        cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose"]
        # sys_prompt foi montado e logado no inicio da funcao (antes do branch
        # mock/real) — propaga via --append-system-prompt.
        cmd.extend(["--append-system-prompt", sys_prompt])
        ov = overrides or {}
        final_model = ov.get("model") or self.model
        final_effort = ov.get("effort") or self.effort
        if ov.get("model") and ov["model"] != self.model:
            log.info(
                "runner.step_overrides_applied",
                agent=self.agent_name, field="model",
                from_=self.model, to=ov["model"],
            )
        if ov.get("effort") and ov["effort"] != self.effort:
            log.info(
                "runner.step_overrides_applied",
                agent=self.agent_name, field="effort",
                from_=self.effort, to=ov["effort"],
            )
        if final_model:
            cmd.extend(["--model", final_model])
        if final_effort:
            cmd.extend(["--effort", final_effort])
        if mcp_config_path.exists():
            cmd.extend(["--mcp-config", str(mcp_config_path), "--strict-mcp-config"])
            if self.allowed_tools:
                cmd.extend(["--allowed-tools", " ".join(self.allowed_tools)])
        # Permite leitura fora do cwd (sandbox bloqueia sem --add-dir).
        # Knowledge do agente incluso pra ficar acessivel mesmo com cwd=worktree.
        extra_dirs = ["/workspace/company", "/workspace/repos"]
        if self.agent_name:
            extra_dirs.append(f"/app/agents/{self.agent_name}/knowledge")
        for extra in extra_dirs:
            if Path(extra).exists():
                cmd.extend(["--add-dir", extra])

        # spawn_cwd = worktree da task se topic eh `task-<slug>` E essa task tem
        # worktree linkada com este agente in_progress; senao workdir (D-97).
        # Permite que Claude Code carregue `.claude/{agents,commands}/`, `CLAUDE.md`
        # do repo nativamente. Identidade do agente vai no `--append-system-prompt`.
        spawn_cwd = await self._resolve_spawn_cwd(workdir, topic_key)

        # `--resume` so funciona se o spawn_cwd casa com o cwd em que o
        # `<resume_sid>.jsonl` foi gravado (Claude CLI guarda projects per-cwd).
        # Quando o cwd resolvido difere do registrado, pulamos `--resume` em vez
        # de mandar pro ghost — o sid permanece no DB pra eventual run futura
        # que volte ao mesmo cwd. Sem `--resume`, esta run perde o buffer da
        # conv CLI mas o restante do contexto vem do system prompt + prompt.
        cwd_str = str(spawn_cwd)
        resume_skipped_reason: str | None = None
        if resume_sid and resume_sid_cwd is not None and resume_sid_cwd != cwd_str:
            resume_skipped_reason = "cwd_mismatch"
            log.warning(
                "runner.resume_skipped",
                topic=topic_key.slug(),
                resume_sid=resume_sid,
                saved_cwd=resume_sid_cwd,
                spawn_cwd=cwd_str,
                reason=resume_skipped_reason,
                hint="sid registrado em outro cwd; spawn fresh, sid preservado no DB",
            )
            resume_sid = None
        if resume_sid:
            cmd.extend(["--resume", resume_sid])

        log.info(
            "runner.spawn",
            topic=topic_key.slug(),
            cwd=cwd_str,
            workdir=str(workdir),
            resume=bool(resume_sid),
            resume_skipped=resume_skipped_reason,
            prompt_len=len(prompt),
            mcp=mcp_config_path.exists(),
        )

        # Claude Code CLI cria auto-memory em ~/.claude/projects/<slug>/memory/
        # por session. Desativamos — usamos memory_save/recall (Postgres) via MCP.
        #
        # Env vars MCP do Claude CLI (dois timeouts distintos — confundi antes):
        #   MCP_TIMEOUT        — startup do server MCP (default 30s). Deixamos o
        #                        default; nossos MCP servers sobem em ms.
        #   MCP_TOOL_TIMEOUT   — per-tool-call timeout. Default ~60s, quebrava
        #                        ask_human (bloqueia aguardando humano, pode
        #                        levar horas) e ask_agent (target pensando ou
        #                        ele proprio bloqueado em ask_human). Subimos
        #                        pra 24h pra cobrir ciclo humano completo
        #                        (dormir/reuniao/viagem). Override via
        #                        MCP_TOOL_TIMEOUT_MS no env.
        spawn_env = {
            **os.environ,
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "MCP_TOOL_TIMEOUT": str(int(os.environ.get("MCP_TOOL_TIMEOUT_MS", 24 * 60 * 60 * 1000))),
        }
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(spawn_cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=spawn_env,
            limit=STREAM_STDOUT_LIMIT_BYTES,
        )
        # D-71: registra proc no dispatcher pra permitir cancel via SIGTERM.
        # Unregister vai no finally do bloco de stream-consume mais abaixo.
        if self._dispatcher is not None:
            try:
                self._dispatcher.handler_register_proc(topic_key, proc)
            except Exception:
                log.exception("runner.register_proc_failed", topic=topic_key.slug())

        result_text = ""
        session_id = resume_sid
        error_subtype: str | None = None
        total_cost_usd: float | None = None
        total_duration_ms: int | None = None
        num_turns: int | None = None
        usage: dict[str, Any] | None = None
        tool_uses: list[str] = []
        # Buffer do ultimo text block do assistant que ainda nao foi emitido
        # como "thinking". A intuicao: texto sem tool_use na sequencia eh a
        # resposta final; emitir como thinking duplicaria o bubble de resposta.
        # Regra: ao ver um tool_use OU um novo text block, flush do buffer
        # anterior como thinking. Se o stream acabar em `result` sem mais
        # tool_use, o buffer pendente eh descartado (nao duplica).
        pending_text_block: str | None = None

        def _flush_pending_thinking():
            nonlocal pending_text_block
            if pending_text_block:
                pt = pending_text_block
                pending_text_block = None
                asyncio.create_task(self._emit_live(
                    topic_key, "thinking", summary=pt,
                ))

        assert proc.stdout is not None
        oversized_lines_skipped = 0
        while True:
            # Loop manual (em vez de `async for`) pra capturar
            # LimitOverrunError/ValueError de uma linha gigante e continuar
            # lendo as proximas. readline() ja drena o buffer interno ate o
            # separator quando estoura, entao a proxima iteracao retoma na
            # linha seguinte. Turn so falha se o `result` final nao chegar.
            try:
                line_bytes = await proc.stdout.readline()
            except (asyncio.LimitOverrunError, ValueError) as e:
                oversized_lines_skipped += 1
                log.warning(
                    "runner.oversized_line_skipped",
                    topic=topic_key.slug(),
                    limit_bytes=STREAM_STDOUT_LIMIT_BYTES,
                    skipped_so_far=oversized_lines_skipped,
                    err=str(e),
                )
                asyncio.create_task(self._emit_live(
                    topic_key, "tool_result",
                    summary=(
                        f"(output truncado: linha do stream-json > "
                        f"{STREAM_STDOUT_LIMIT_BYTES // (1024*1024)} MiB "
                        "foi descartada pelo framework)"
                    ),
                    data={"is_error": True, "oversized": True},
                ))
                continue
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                log.debug("runner.non_json_line", line=line[:200])
                continue

            t = obj.get("type")
            if t == "system" and obj.get("subtype") == "init":
                session_id = obj.get("session_id") or session_id
                asyncio.create_task(self._emit_live(
                    topic_key, "run_start",
                    summary=f"resume={bool(resume_sid)} model={self.model or '(default)'}",
                ))
            elif t == "assistant":
                msg = obj.get("message") or {}
                for block in msg.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        # Texto anterior pendente eh "thinking de verdade" — flush.
                        _flush_pending_thinking()
                        name = str(block.get("name", "?"))
                        tool_uses.append(name)
                        inp = block.get("input") or {}
                        first_kv = next(iter(inp.items()), None)
                        snippet = ""
                        if first_kv:
                            v = str(first_kv[1])
                            if len(v) > 80:
                                v = v[:77] + "..."
                            snippet = f" {first_kv[0]}={v}"
                        # input completo em `data.input` — frontend usa pra
                        # click-to-expand no LiveEventLine. Cap defensivo pra
                        # nao estourar payload do SSE.
                        try:
                            input_json = json.dumps(inp, ensure_ascii=False, default=str)
                        except Exception:
                            input_json = str(inp)
                        if len(input_json) > 20000:
                            input_json = input_json[:19997] + "..."
                        asyncio.create_task(self._emit_live(
                            topic_key, "tool_use",
                            summary=f"{name}{snippet}",
                            data={"tool": name, "input": input_json},
                        ))
                    elif block.get("type") == "text":
                        text = (block.get("text") or "").strip()
                        if text:
                            # Se tinha texto pendente anterior, esse eh thinking
                            # confirmado (ha um text depois no fluxo). Flush.
                            _flush_pending_thinking()
                            # Esse novo text fica em buffer — so vira "thinking"
                            # se um tool_use aparecer depois. Se `result` vier
                            # antes, eh a resposta final e nao emitimos (dedup
                            # contra o bubble da mensagem do bot).
                            pending_text_block = text
            elif t == "user":
                # tool_result blocks vem como user msg apos o claude rodar a tool
                msg = obj.get("message") or {}
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        is_err = block.get("is_error", False)
                        # Extrai texto sempre (sucesso + erro) pra frontend
                        # exibir output no OUT expandido. content pode ser
                        # string ou list[{type:text, text:...}].
                        content = block.get("content")
                        out_text = ""
                        if isinstance(content, str):
                            out_text = content
                        elif isinstance(content, list):
                            parts = []
                            for c in content:
                                if isinstance(c, dict):
                                    parts.append(c.get("text") or c.get("content") or "")
                                else:
                                    parts.append(str(c))
                            out_text = "\n".join(p for p in parts if p)
                        out_text = out_text.strip()
                        if is_err:
                            summary = out_text or "error"
                        else:
                            summary = "ok"
                        # Cap inline em ~5KB pra caber no pg_notify (limite 8KB
                        # inclui wrapper JSON + id/agent/ts/kind/summary).
                        # Versao full fica em `output_full` (so usado se
                        # truncado); trigger strip-a esse campo antes de
                        # emitir o NOTIFY pra nao estourar. Frontend busca
                        # full via GET /api/live_events/{id}/full.
                        OUTPUT_INLINE_CAP = 5000
                        OUTPUT_FULL_CAP = 200_000  # protege banco/transporte
                        full_text = out_text[:OUTPUT_FULL_CAP]
                        is_truncated = len(out_text) > OUTPUT_INLINE_CAP
                        data_payload: dict = {
                            "is_error": is_err,
                            "output": out_text[:OUTPUT_INLINE_CAP],
                        }
                        if is_truncated:
                            data_payload["output_truncated"] = True
                            data_payload["output_full_len"] = len(out_text)
                            # Arquiva completo no banco (trigger strip-a antes do NOTIFY).
                            data_payload["output_full"] = full_text
                        asyncio.create_task(self._emit_live(
                            topic_key, "tool_result",
                            summary=summary,
                            data=data_payload,
                        ))
            elif t == "result":
                # NAO flush do pending_text_block aqui — se ele nao foi
                # interrompido por tool_use, ele e a resposta final e vai
                # chegar como result_text abaixo. Emitir como thinking
                # duplicaria o bubble da resposta (F3 dedup).
                pending_text_block = None
                result_text = obj.get("result", "") or ""
                session_id = obj.get("session_id") or session_id
                if obj.get("subtype") != "success":
                    error_subtype = obj.get("subtype")
                total_cost_usd = obj.get("total_cost_usd")
                total_duration_ms = obj.get("duration_ms") or obj.get("total_duration_ms")
                num_turns = obj.get("num_turns")
                usage = obj.get("usage")
                cost_str = f"${total_cost_usd:.4f}" if total_cost_usd is not None else "?"
                asyncio.create_task(self._emit_live(
                    topic_key, "run_end",
                    summary=f"turns={num_turns or '?'} cost={cost_str} dur={total_duration_ms or '?'}ms",
                    # Campos estruturados pra UI somar no header do topic.
                    data={
                        "tool_uses": tool_uses,
                        "subtype": obj.get("subtype"),
                        "cost_usd": total_cost_usd,
                        "duration_ms": total_duration_ms,
                        "num_turns": num_turns,
                    },
                ))

        rc = await proc.wait()
        stderr_bytes = await proc.stderr.read() if proc.stderr else b""
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")

        # D-71: proc encerrou — libera a ref pro dispatcher nao tentar SIGTERM
        # num proc ja morto. Idempotente; ProcessLookupError ja e tolerado no
        # caminho de cancel tambem.
        if self._dispatcher is not None:
            try:
                self._dispatcher.handler_unregister_proc(topic_key)
            except Exception:
                log.debug("runner.unregister_proc_failed", topic=topic_key.slug())

        # D-71: garantir que o badge do PWA transiciona pra fora de "running".
        # O `run_end` live_event normal e emitido quando o CLI emite
        # `type=result` no stream-json. Em kill hard (SIGKILL) ou crash, o
        # CLI sai sem emitir result — o frontend continuava vendo
        # `runner_state=running` indefinidamente. Detectamos via
        # `total_duration_ms is None` (setado so no path do result) e
        # emitimos run_end sintetico com subtype=error.
        if total_duration_ms is None and rc != 0:
            synthetic_subtype = "killed" if rc in (-15, 143, -9, 137) else "error"
            stderr_preview = stderr_text.strip().splitlines()[-1] if stderr_text.strip() else ""
            asyncio.create_task(self._emit_live(
                topic_key, "run_end",
                summary=f"exit={rc} subtype={synthetic_subtype}"
                        + (f" · {stderr_preview[:160]}" if stderr_preview else ""),
                data={
                    "subtype": synthetic_subtype,
                    "synthetic": True,
                    "rc": rc,
                    "tool_uses": tool_uses,
                },
            ))

        # Spurious exit code: o stream-json devolveu `result subtype=success`
        # (i.e. trabalho completo + duration_ms preenchido + sem error_subtype),
        # mas o processo morreu rc != 0. Bug do CLI no shutdown (cleanup de MCP /
        # fd / async task). Reportar "❌ erro no claude" nesse caso confunde o
        # humano e descarta um turn que de fato deu certo. Tratamos como ok e
        # apenas logamos pra ter sinal sem mascarar caso vire epidemia.
        spurious_exit = (
            rc != 0 and total_duration_ms is not None and not error_subtype
        )
        if spurious_exit:
            log.warning(
                "runner.spurious_exit_code",
                topic=topic_key.slug(),
                rc=rc,
                num_turns=num_turns,
                duration_ms=total_duration_ms,
                hint="result success no stream mas processo saiu rc!=0",
            )

        ghost_session = bool(GHOST_SESSION_RE.search(stderr_text))

        # Persistencia defensiva do session_id (D-70): o CLI emite um novo id
        # no `system/init` de toda invocacao (fork ao resumir), mas so grava o
        # .jsonl correspondente quando a run avanca. Se a run morre logo apos
        # o init, o id existe no runtime mas nunca toca o disco — salva-lo no
        # DB cria um ghost que quebra a proxima tentativa. Pulamos o save nesse
        # caso e mantemos o id anterior (que sabidamente existe).
        if session_id and session_id != resume_sid and not _session_jsonl_exists(session_id):
            log.warning(
                "runner.session_id_ghost",
                topic=topic_key.slug(),
                session_id=session_id,
                previous=resume_sid,
                rc=rc,
            )
        elif session_id:
            # D-97: persiste cwd junto pra runs futuras detectarem mismatch
            # antes de tentar `--resume`.
            await self.session_mgr.save_session_id(topic_key, session_id, cwd=cwd_str)

        return RunOutcome(
            ok=(
                (rc == 0 or spurious_exit)
                and not error_subtype
                and not (oversized_lines_skipped and not result_text)
            ),
            rc=rc,
            result_text=result_text,
            session_id=session_id,
            error_subtype=error_subtype,
            stderr=stderr_text,
            total_cost_usd=total_cost_usd,
            duration_ms=total_duration_ms,
            num_turns=num_turns,
            usage=usage,
            tool_uses=tool_uses,
            oversized_lines_skipped=oversized_lines_skipped,
            ghost_session=ghost_session,
        )

    async def handle(self, event: dict[str, Any], topic_key: TopicKey, workdir: Path) -> None:
        raw = str(event.get("content") or "")
        prompt = MENTION_RE.sub("", raw).strip()
        if not prompt:
            log.info("runner.empty_prompt", topic=topic_key.slug())
            return

        # Stale handoff filter: handoffs postados pelo reactor durante uma run
        # que ja absorveu a fase via --resume continuity ficam na fila do
        # dispatcher (LISTEN dispara mid-run, dispatcher enfileira). Quando a
        # run principal acaba, dispatcher consome essa fila — cada handoff
        # vira um spawn novo cuja unica saida eh "atrasado, ja feito".
        # Sintoma: ops/2026-05-05 fix-task-16 (2 runs cascade pos-`done`).
        # Drop quando: (1) task em status terminal, ou (2) a fase mais
        # recente desta step ja foi concluida (`completed_at IS NOT NULL`).
        # Olhamos a `phases` mais recente por idx — preserva reopen_task,
        # que insere phase nova in-flight pra mesma step ja antes concluida
        # (filter precisa nao dropar nesse caso). Cursor avanca normalmente
        # via mark_processed pos-handle.
        if self.db_pool is not None:
            ho = HANDOFF_BODY_RE.search(prompt)
            if ho:
                ho_slug = ho.group(1)
                ho_step = ho.group(2)
                try:
                    async with self.db_pool.acquire() as conn:
                        row = await conn.fetchrow(
                            """SELECT t.status,
                                      (
                                        SELECT p.completed_at IS NOT NULL
                                          FROM tasks.phases p
                                         WHERE p.task_id = t.id AND p.step = $2
                                         ORDER BY p.idx DESC LIMIT 1
                                      ) AS latest_phase_done
                                 FROM tasks.tasks t WHERE t.slug = $1""",
                            ho_slug, ho_step,
                        )
                except Exception as e:
                    log.warning(
                        "runner.stale_handoff_check_failed",
                        topic=topic_key.slug(), error=str(e)[:200],
                    )
                    row = None
                if row is not None:
                    is_terminal = row["status"] in ("done", "blocked", "human_review")
                    latest_done = bool(row["latest_phase_done"])
                    if is_terminal or latest_done:
                        log.info(
                            "runner.stale_handoff_dropped",
                            topic=topic_key.slug(),
                            task_slug=ho_slug,
                            handoff_step=ho_step,
                            task_status=row["status"],
                            latest_phase_done=latest_done,
                        )
                        return

        started_at = int(time.time())
        # Resolve step overrides 1x por handle. Lido do `workflows.yaml`
        # via `_step_overrides`. Vazio se topic nao-task / step sem overrides.
        # Propagado pra `_inject_memory` e `_run_claude_once` em todos os
        # attempts deste handle (config nao muda mid-handle).
        step_overrides = await self._step_overrides(topic_key)
        # Auto-inject de memoria relevante (se configurado, com overrides).
        prompt = await self._inject_memory(prompt, overrides=step_overrides)

        # Conv id do evento — usado pelo broker (D-87) e pelos blocos
        # contextuais do system prompt (modo de invocacao, estado da task).
        conv_id_raw = event.get("conversation_id") if isinstance(event, dict) else None
        try:
            conv_id_for_run = int(conv_id_raw) if conv_id_raw is not None else None
        except (TypeError, ValueError):
            conv_id_for_run = None

        if self.broker is not None:
            # D-87: propaga conv_id pro broker interno. Usado depois pelo MCP
            # handler de `__ask_agent` pra passar parent_conv_id na criacao
            # da conv filha `__ask-from-*` — hierarquia persistida via schema,
            # sem heuristica.
            self.broker.register_topic(topic_key, conv_id=conv_id_for_run)

        # (Antes havia ack "thinking..." aqui — removido. PWA hoje mostra
        # tool_use/thinking ao vivo via live_events, tornando o ack ruido.)

        # MCP config: servidor in-process + merge de instance/agents/<name>/mcp.extra.json
        # (capabilities laterais como playwright-mcp) se existir.
        mcp_config_path = workdir / ".mcp-config.json"
        if self.broker is not None and self.mcp_url_for is not None:
            url = self.mcp_url_for(topic_key.slug())
            servers: dict = {MCP_SERVER_NAME: {"type": "http", "url": url}}
            if self.agent_name:
                extra_path = Path(f"/app/agents/{self.agent_name}/mcp.extra.json")
                if extra_path.exists():
                    try:
                        extra = json.loads(extra_path.read_text())
                        extra_servers = extra.get("mcpServers") or {}
                        if isinstance(extra_servers, dict):
                            servers.update(extra_servers)
                    except Exception:
                        log.warning(
                            "runner.mcp_extra_parse_failed",
                            path=str(extra_path),
                        )
            mcp_config_path.write_text(
                json.dumps({"mcpServers": servers}, indent=2)
            )
            log.debug(
                "runner.mcp_config",
                topic=topic_key.slug(),
                url=url,
                extra_servers=sorted(s for s in servers if s != MCP_SERVER_NAME),
            )

        # Loop de tentativas — re-resolve resume_sid entre tentativas
        # (primeira run pode salvar session_id antes de ser killed).
        outcome: RunOutcome | None = None
        attempts_done = 0
        # Ghost session recovery (D-70): se o CLI reclama "No conversation
        # found with session ID" (resume apontando pra id inexistente em
        # disco), limpamos o DB e refazemos a MESMA tentativa sem `--resume`,
        # sem consumir um attempt normal. Uma unica recovery por handle —
        # se falhar de novo, cai no retry normal.
        ghost_recovered = False
        attempt = 0
        while attempt < MAX_ATTEMPTS:
            attempt += 1
            attempts_done = attempt
            session_ref = await self.session_mgr.session_ref_for(topic_key)
            resume_sid = session_ref[0] if session_ref else None
            resume_sid_cwd = session_ref[1] if session_ref else None
            # Retry pos-oversized: troca o prompt pelo recovery (pede leitura
            # parcial) e so faz sentido com --resume (pra Claude ter o contexto
            # do que estava fazendo). Se nao tem session_id ainda, caimos no
            # prompt original — melhor do que mandar recovery sem contexto.
            current_prompt = prompt
            if (
                attempt > 1
                and outcome is not None
                and outcome.oversized_lines_skipped
                and resume_sid
            ):
                current_prompt = OVERSIZED_RECOVERY_PROMPT
            outcome = await self._run_claude_once(
                prompt=current_prompt,
                workdir=workdir,
                mcp_config_path=mcp_config_path,
                resume_sid=resume_sid,
                resume_sid_cwd=resume_sid_cwd,
                topic_key=topic_key,
                conv_id=conv_id_for_run,
                overrides=step_overrides,
            )
            if outcome.ok:
                break
            # Ghost session: CLI apontou pra sid inexistente. Limpa o DB pra
            # proxima leitura pegar None (→ spawn sem `--resume`, fresh start)
            # e repete este attempt sem consumi-lo. So uma vez pra evitar loop.
            if outcome.ghost_session and not ghost_recovered and resume_sid:
                ghost_recovered = True
                await self.session_mgr.clear_session_id(topic_key)
                log.warning(
                    "runner.ghost_session_recover",
                    topic=topic_key.slug(),
                    missing_sid=resume_sid,
                    hint="retry sem --resume; contexto da conversa perdido, artefatos em disco preservam estado",
                )
                attempt -= 1  # nao conta este attempt — sera refeito fresh
                continue
            # D-72: se o rc=143 veio de cancel_topic do humano (SIGTERM via
            # /api/cancel ou cascade delete), NAO retenta. O dispatcher
            # marcou o topic via `_user_cancelled` no SIGTERM — consumimos
            # aqui pra pular retry e deixar o turn morrer.
            if (
                self._dispatcher is not None
                and outcome.retriable
                and self._dispatcher.consume_user_cancel(topic_key)
            ):
                log.info(
                    "runner.user_cancelled",
                    topic=topic_key.slug(),
                    attempt=attempt,
                    rc=outcome.rc,
                    hint="SIGTERM veio de cancel do humano; skip retry",
                )
                break
            if not outcome.retriable or attempt == MAX_ATTEMPTS:
                break
            log.warning(
                "runner.retrying",
                topic=topic_key.slug(),
                attempt=attempt,
                rc=outcome.rc,
                error_subtype=outcome.error_subtype,
                oversized_lines_skipped=outcome.oversized_lines_skipped,
                will_resume=bool(outcome.session_id),
            )
            await asyncio.sleep(RETRY_BACKOFF_SEC * attempt)

        assert outcome is not None
        usage = outcome.usage or {}
        # Chaves alinhadas com backend /api/telemetry/event (main.py):
        #   payload.get("topic_slug"), payload.get("cost_usd"), etc.
        # Metadados extras (num_turns, tool_uses, ...) caem no JSON metadata.
        # Os 3 contadores de input do Claude CLI sao disjuntos (mesma semantica
        # da API da Anthropic) — armazenamos cada um na sua coluna. Migration 010
        # reverteu a soma que 004 introduzia em `input_tokens`.
        base_input = usage.get("input_tokens") or 0
        cache_creation = usage.get("cache_creation_input_tokens") or 0
        cache_read = usage.get("cache_read_input_tokens") or 0
        # task_slug: se topic comeca com 'task-', extrai o slug da task. Backend
        # resolve conversation_id via (stream, topic) — mesma pratica do live-event.
        task_slug = topic_key.topic[5:] if topic_key.topic.startswith("task-") else None
        telemetry = {
            "topic_slug": topic_key.slug(),
            "stream": topic_key.stream,
            "topic": topic_key.topic,
            "task_slug": task_slug,
            "event_type": "run_end" if outcome.ok else "run_error",
            "duration_ms": outcome.duration_ms,
            "cost_usd": outcome.total_cost_usd,
            "input_tokens": base_input,
            "output_tokens": usage.get("output_tokens"),
            "cache_creation_tokens": cache_creation,
            "cache_read_tokens": cache_read,
            "model": self.model,
            "metadata": {
                "num_turns": outcome.num_turns,
                "started_at": started_at,
                "tool_uses": outcome.tool_uses,
                "exit_code": outcome.rc or 0,
                "error_subtype": outcome.error_subtype,
                "oversized_lines_skipped": outcome.oversized_lines_skipped,
            },
        }

        if not outcome.ok:
            log.warning(
                "runner.claude_failed",
                topic=topic_key.slug(),
                rc=outcome.rc,
                error_subtype=outcome.error_subtype,
                attempts=attempts_done,
                oversized_lines_skipped=outcome.oversized_lines_skipped,
                stderr=outcome.stderr[:500],
            )
            await self._emit_telemetry(telemetry)
            retried_note = f" (apos {attempts_done} tentativas)" if attempts_done > 1 else ""
            err_msg = (
                f"❌ erro no claude{retried_note} "
                f"(exit={outcome.rc}, subtype={outcome.error_subtype})\n"
                f"```\n{outcome.stderr[:800] or '(sem stderr)'}\n```"
            )
            await self._reply(topic_key, err_msg)
            return

        final = outcome.result_text or "_(resposta vazia)_"
        log.info(
            "runner.done",
            topic=topic_key.slug(),
            out_len=len(final),
            session_id=outcome.session_id,
            total_cost_usd=outcome.total_cost_usd,
            duration_ms=outcome.duration_ms,
            num_turns=outcome.num_turns,
            attempts=attempts_done,
            oversized_lines_skipped=outcome.oversized_lines_skipped,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_read_tokens=usage.get("cache_read_input_tokens"),
            cache_creation_tokens=usage.get("cache_creation_input_tokens"),
            tool_uses=outcome.tool_uses,
        )
        await self._emit_telemetry(telemetry)
        await self._reply(topic_key, final, tool_uses=outcome.tool_uses)

    async def _reply(
        self,
        key: TopicKey,
        content: str,
        tool_uses: list[str] | None = None,
    ) -> None:
        # Idempotency key por turn: protege contra reentrancia/retry do
        # `_reply` que poderia inserir 2x a mesma msg na conv (race ou
        # exception parcial). Schema tem UNIQUE(conversation_id, client_id)
        # — broker devolve a msg existente em conflito. Defesa barata.
        import uuid as _uuid
        reply_uid = _uuid.uuid4().hex[:16]
        try:
            await self.broker_client.send_message(
                key.stream, key.topic, content,
                client_id=f"reply-{reply_uid}",
            )
        except Exception:
            log.exception("runner.reply_failed")
            return
        # D-100: se este turn rodou em conv filha (parent_conv_id IS NOT NULL)
        # criada por handoff via complete_phase, ecoa o reply final na conv
        # pai. Antes, o pai (PO) so via a resposta se tivesse delegado via
        # ask_agent — e nesse caminho o asker faz subscribe_to_conversation
        # na conv `__child-*` criada pelo broker. Pra handoff de fase
        # (`task-<slug>` no stream do filho), nao havia subscricao
        # equivalente — o reply ficava orfao no banco. Skip topics `__*`
        # porque ask_agent ja tem auto-route via subscribe_to_conversation
        # e gerar duplicata.
        if key.topic.startswith("__"):
            return
        # Skip echo se este turn chamou `complete_phase`. O reactor ja vai
        # postar um handoff (ou terminal) na conv pai com o summary do
        # complete_phase — ecoar o reply do filho duplica a entrega ao pai
        # e faz ele acordar 2x pra mesmo entregavel. Sintoma observado em
        # 2026-04-28 na task fix-login-redirect (PO recebeu
        # "Handoff from executor-go" em seguida de "Reply from executor-go"
        # com mesmo conteudo, processou 2 turns e respondeu "esse reply e
        # uma confirmacao redundante"). Replies sem complete_phase
        # (ex: filho devolveu duvida em vez de despachar) continuam ecoando.
        completed_phase = (
            tool_uses is not None
            and f"mcp__{MCP_SERVER_NAME}__complete_phase" in tool_uses
        )
        if completed_phase:
            log.debug(
                "runner.echo_skipped_complete_phase",
                topic=key.slug(),
            )
            return
        if self.db_pool is None:
            return
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT pc.id AS parent_id,
                              ps.name AS parent_stream,
                              pc.topic_name AS parent_topic
                         FROM messaging.conversations c
                         JOIN messaging.streams s ON s.id = c.stream_id
                         JOIN messaging.conversations pc ON pc.id = c.parent_conv_id
                         JOIN messaging.streams ps ON ps.id = pc.stream_id
                        WHERE s.name = $1 AND c.topic_name = $2""",
                    key.stream, key.topic,
                )
        except Exception:
            log.exception("runner.parent_lookup_failed", topic=key.slug())
            return
        if row is None:
            return
        agent_label = self.agent_name or key.stream
        forwarded = (
            f"🗨️ **Reply from `{agent_label}`** "
            f"(`{key.stream}` / `{key.topic}`)\n\n{content}"
        )
        try:
            await self.broker_client.send_message(
                row["parent_stream"], row["parent_topic"], forwarded,
                client_id=f"echo-{reply_uid}",
                kind="echo",
            )
            log.info(
                "runner.forwarded_to_parent",
                child_topic=key.slug(),
                parent_stream=row["parent_stream"],
                parent_topic=row["parent_topic"],
            )
        except Exception:
            log.exception(
                "runner.forward_to_parent_failed",
                child_topic=key.slug(),
                parent_stream=row["parent_stream"],
                parent_topic=row["parent_topic"],
            )
