"""WorkflowManager — protocolo generico de tasks multi-fase.

Este modulo e **agnostico a vocabulario**: ele nao sabe o que e "triagem",
"analise", "coordenador", etc. A instancia (empresa) declara seus steps,
agentes default, artifacts e transicoes em `company/workflows.yaml`. O
framework carrega em runtime, valida contra a declaracao e resolve
roteamento.

O framework conhece APENAS:
  - Tasks, com slug kebab-case, vivem no Postgres (schema `tasks`).
  - Artifacts (os .md produzidos em cada fase) continuam em
    `company/tasks/<slug>/` — agentes escrevem via Write tool do CLI,
    PWA le e renderiza.
  - Uma fase tem um `step` (string livre, definido pela instancia).
  - Transicoes terminais com semantica fixa: `done` | `halt` | `human_review`
    — esses nomes sao parte do protocolo porque mudam status e dispatch
    no reactor. Instancias nao podem redefinir.
  - `metadata.workflow: <name>` seleciona qual workflow da company rege
    a task. Se nao existe workflows.yaml ou o name nao bate, o framework
    roda em **modo permissivo** — aceita qualquer step mas nao resolve
    agent default nem valida transicao.

Events em Postgres (orchestrator.events); reactor consome via LISTEN.
Schema de tasks.* adicionado pela migration 006 (D-53).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg
import yaml

from ..log import get_logger

log = get_logger(__name__)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Terminais de protocolo — parte do framework, nao da instancia.
# Cada um tem semantica distinta pro reactor:
#   done          => status=done       (sucesso)
#   halt          => status=blocked    (pausa — humano desbloqueia)
#   human_review  => status=human_review (escala — humano decide direcao)
TERMINALS: set[str] = {"done", "halt", "human_review"}


class WorkflowError(ValueError):
    """Erro de protocolo — retorna como JSONRPC_INVALID_PARAMS."""


# ---------- Definicao de workflow carregada de workflows.yaml ----------


@dataclass(frozen=True)
class StepDef:
    """Declaracao de um step segundo a instancia."""
    name: str
    agent: str | None               # None => exige next_agent na chamada
    artifact: str | None            # default de arquivo produzido neste step
    next: frozenset[str]            # steps/terminais validos como proximo
    # Quando True, ao entrar neste step o framework limpa o claude_session_id
    # da conv destino (stream, topic) — proximo spawn roda `claude -p` sem
    # `--resume`, com janela de contexto fresca. As `instructions` do step
    # passam via append-system-prompt como sempre (build dinamico). Default
    # False: mantem sessao via --resume (comportamento legado).
    fresh_session: bool = False


@dataclass(frozen=True)
class WorkflowDef:
    name: str
    initial_step: str
    steps: dict[str, StepDef] = field(default_factory=dict)
    # Agente que orquestra esse workflow — dono da conv-supervisora que
    # `backlog_promote` cria. NULL => fallback pra `initial_step.agent`
    # (compat com workflows pre-orchestrator). Diferente do
    # `initial_step.agent`: o orchestrator pode nao tocar a primeira fase
    # mas continua sendo a raiz das convs do task. Ex: workflow de deploy
    # tem orchestrator=`deploy-coordinator`, mas a primeira fase pode ir
    # direto pra `executor-test`.
    orchestrator: str | None = None

    def step(self, name: str) -> StepDef | None:
        return self.steps.get(name)


class WorkflowRegistry:
    """Carrega workflows.yaml da instancia e entrega WorkflowDef por nome.

    Leitura soh na hora da chamada (nao faz cache hit-the-disk-once) —
    reconcile/reload e barato e permite editar workflows.yaml sem restart.
    """

    def __init__(self, workflows_path: Path):
        self.workflows_path = workflows_path

    def load(self) -> dict[str, WorkflowDef]:
        if not self.workflows_path.exists():
            return {}
        raw = yaml.safe_load(self.workflows_path.read_text(encoding="utf-8")) or {}
        wfs_raw = raw.get("workflows") or {}
        out: dict[str, WorkflowDef] = {}
        for wf_name, wf_body in wfs_raw.items():
            if not isinstance(wf_body, dict):
                continue
            steps_raw = wf_body.get("steps") or {}
            steps: dict[str, StepDef] = {}
            for s_name, s_body in steps_raw.items():
                if not isinstance(s_body, dict):
                    continue
                steps[s_name] = StepDef(
                    name=s_name,
                    agent=s_body.get("agent"),
                    artifact=s_body.get("artifact"),
                    next=frozenset(s_body.get("next") or []),
                    fresh_session=bool(s_body.get("fresh_session", False)),
                )
            initial_step_name = str(wf_body.get("initial_step") or "")
            # orchestrator: campo dedicado; sem ele, fallback pro agente do
            # initial_step (compat com workflows antigos).
            orchestrator = wf_body.get("orchestrator")
            if not orchestrator and initial_step_name in steps:
                orchestrator = steps[initial_step_name].agent
            out[wf_name] = WorkflowDef(
                name=wf_name,
                initial_step=initial_step_name,
                steps=steps,
                orchestrator=orchestrator,
            )
        return out

    def get(self, name: str | None) -> WorkflowDef | None:
        if not name:
            return None
        return self.load().get(name)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------- WorkflowManager ----------


class WorkflowManager:
    """Protocolo generico — delega taxonomia pra WorkflowRegistry.

    Persistencia em Postgres (tasks.tasks, tasks.phases, tasks.worktrees).
    `company_dir` ainda eh passado pra resolver `workflows.yaml` e pro
    diretorio de artifacts `company/tasks/<slug>/` (onde os .md vivem).
    """

    def __init__(self, company_dir: Path, db_pool: asyncpg.Pool):
        self.company_dir = company_dir
        self.tasks_dir = company_dir / "tasks"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self._pool = db_pool
        self.registry = WorkflowRegistry(company_dir / "workflows.yaml")

    # ---------- helpers ----------

    def _task_dir(self, slug: str) -> Path:
        """Path de artifacts (.md) da task no filesystem — metadata vive no banco."""
        return self.tasks_dir / slug

    async def _load_task_row(self, conn: asyncpg.Connection, slug: str) -> dict[str, Any] | None:
        row = await conn.fetchrow(
            """SELECT id, slug, title, workflow, status, current_step, current_agent,
                      complexity, impact, difficulty, origin, origin_stream, origin_topic,
                      blocked_reason, metadata_extra, archived_at, created_at, updated_at
                 FROM tasks.tasks WHERE slug = $1""",
            slug,
        )
        return dict(row) if row else None

    async def _load_phases(self, conn: asyncpg.Connection, task_id: int) -> list[dict[str, Any]]:
        rows = await conn.fetch(
            """SELECT idx, step, agent, started_at, completed_at, artifact, summary
                 FROM tasks.phases WHERE task_id = $1 ORDER BY idx ASC""",
            task_id,
        )
        return [dict(r) for r in rows]

    async def _load_worktrees(self, conn: asyncpg.Connection, task_id: int) -> dict[str, dict]:
        rows = await conn.fetch(
            """SELECT repo, branch, path, created_at
                 FROM tasks.worktrees WHERE task_id = $1""",
            task_id,
        )
        return {
            r["repo"]: {
                "repo": r["repo"],
                "branch": r["branch"],
                "path": r["path"],
                "registered_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        }

    def _resolve_workflow(self, workflow_name: str | None) -> WorkflowDef | None:
        return self.registry.get(workflow_name)

    def _validate_transition(
        self,
        wf: WorkflowDef | None,
        current_step: str | None,
        next_: str,
    ) -> None:
        """Valida transicao contra o workflow declarado. Modo permissivo
        (aceita tudo) se nao ha wf ou o step nao esta declarado."""
        if wf is None or current_step is None:
            return  # modo permissivo
        step = wf.step(current_step)
        if step is None:
            return  # step ausente no wf — permissivo
        if next_ not in step.next:
            allowed = sorted(step.next)
            raise WorkflowError(
                f"Invalid transition in workflow '{wf.name}': from '{current_step}' "
                f"you can only go to {allowed}. Attempted '{next_}'."
            )

    def _resolve_next_agent(
        self,
        wf: WorkflowDef | None,
        next_: str,
        next_agent_override: str | None,
    ) -> tuple[str | None, bool]:
        """Retorna (agent_resolved_or_override, is_terminal).

        Ordem de resolucao:
          1. terminal → (None, True).
          2. next_agent_override sempre vence.
          3. wf.step(next_).agent se declarado.
          4. erro: exige override.
        """
        if next_ in TERMINALS:
            return None, True
        if next_agent_override:
            return next_agent_override, False
        if wf is not None:
            step = wf.step(next_)
            if step is not None:
                if step.agent:
                    return step.agent, False
                raise WorkflowError(
                    f"Step '{next_}' in workflow '{wf.name}' has no default agent "
                    "declared. Pass an explicit next_agent."
                )
        # No workflow declared or step undefined — override required.
        raise WorkflowError(
            f"Could not resolve agent for step '{next_}'. Declare it in "
            "company/workflows.yaml (with field `agent`) or pass an explicit next_agent."
        )

    def _expected_artifact_for(self, wf: WorkflowDef | None, step_name: str) -> str | None:
        if wf is None:
            return None
        s = wf.step(step_name)
        return s.artifact if s else None

    @staticmethod
    def _resolve_handoff_topic(
        *,
        explicit_topic: str | None,
        next_agent: str | None,
        task_slug: str,
        origin_stream: str | None,
        origin_topic: str | None,
        standalone: bool = False,
    ) -> str:
        """Escolhe o topic onde o handoff vai ser postado.

        Prioridade:
          1. `explicit_topic` (override passado pelo chamador).
          2. `standalone=True` -> sempre `task-<slug>`. Fan-out pede
             isolamento por design; rule (3) abaixo nao se aplica porque
             o humano *quer* uma conv nova.
          3. Se o handoff volta pro agente de origem (o que abriu a task),
             usa o topic onde o humano pediu — mantém humano e agente na
             mesma thread ao longo do ciclo de vida da task.
          4. Fallback `task-<slug>`.

        Regra (3) evita o modo de falha em que o retorno pro origin-agent
        cai num topic novo `task-<slug>` que o humano nem sabe que existe;
        se o agente esquecer de chamar `ask_human`, a mensagem fica
        silenciada. Posta direto na conversa que ja esta aberta.

        Mas no caso ops (mega-agente, agent==origin_stream sempre)
        a rule (3) dispararia em todo handoff, inclusive fan-out, e
        colapsaria as N tasks paralelas na conv original. `standalone`
        sinaliza esse contexto e desativa o fold-back.
        """
        if explicit_topic:
            return explicit_topic
        if standalone:
            return f"task-{task_slug}"
        if (
            next_agent
            and origin_stream
            and origin_topic
            and next_agent == origin_stream
        ):
            return origin_topic
        return f"task-{task_slug}"

    # ---------- API principal ----------

    async def complete_phase(
        self,
        *,
        task_slug: str,
        artifact: str,
        summary: str,
        next_: str,
        agent_name: str,
        next_agent: str | None = None,
        next_topic: str | None = None,
        origin_stream: str | None = None,
        origin_topic: str | None = None,
        title: str | None = None,
        workflow: str | None = None,
        complexity: str | None = None,
        impact: str | None = None,
        difficulty: str | None = None,
        origin: str | None = None,
        baseline: dict[str, str] | None = None,
        standalone: bool = False,
    ) -> dict[str, Any]:
        """Registra fim da step atual e dispara handoff pro proximo step.

        Primeira chamada cria a task (schema permite upsert). `current_step`
        eh lido do banco. Se nao existir, vem do `workflow.initial_step`.
        Modo permissivo (sem workflow declarado) usa 'start' como fallback.

        Tudo em uma transacao: UPSERT tasks, INSERT phases, INSERT
        orchestrator.events. Se algo falhar, rollback atomico.
        """
        if not SLUG_RE.match(task_slug):
            raise WorkflowError(
                f"invalid task_slug: {task_slug!r}. Use kebab-case without accents."
            )

        # Artifacts continuam no filesystem — complete_phase exige que o arquivo
        # exista antes de registrar a fase no banco (evita phases orfas).
        task_dir = self._task_dir(task_slug)
        task_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = task_dir / artifact
        if not artifact_path.exists():
            raise FileNotFoundError(
                f"Artifact '{artifact}' does not exist in {task_dir}. "
                "Create the file BEFORE calling complete_phase."
            )

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                task_row = await self._load_task_row(conn, task_slug)
                is_first_call = task_row is None

                # Workflow vem do banco (se task existe) ou do argumento (create).
                wf_name = (task_row or {}).get("workflow") or workflow
                wf = self._resolve_workflow(wf_name)

                # Resolve current_step.
                # Ordem: banco > ultima fase concluida > initial_step do wf > 'start'.
                #
                # O fallback pra "ultima fase concluida" e critico em tasks que
                # ja passaram por terminal nao-`done` (halt/human_review). Nesses
                # casos `current_step` no banco esta NULL (UPSERT seta NULL pra
                # is_terminal); usar `initial_step` faria um `complete_phase`
                # subsequente (ex: PO indo pra `encerramento` apos human_review)
                # registrar `from_step=triagem` no payload, gerando handoff com
                # "Previous step: triagem" errado.
                current_step = (task_row or {}).get("current_step")
                if not current_step and task_row is not None:
                    last_done_step = await conn.fetchval(
                        """SELECT step FROM tasks.phases
                            WHERE task_id = $1 AND completed_at IS NOT NULL
                            ORDER BY idx DESC LIMIT 1""",
                        task_row["id"],
                    )
                    if last_done_step:
                        current_step = last_done_step
                if not current_step:
                    current_step = wf.initial_step if wf and wf.initial_step else "start"

                self._validate_transition(wf, current_step, next_)
                resolved_agent, is_terminal = self._resolve_next_agent(wf, next_, next_agent)

                now = datetime.now(tz=timezone.utc)

                # Determina started_at da fase concluida. Se ja existe fase
                # em aberto (completed_at NULL), usa o started_at dela. Senao,
                # usa created_at da task (primeira fase).
                in_flight = await conn.fetchrow(
                    """SELECT id, idx, started_at FROM tasks.phases
                        WHERE task_id = (SELECT id FROM tasks.tasks WHERE slug = $1)
                          AND completed_at IS NULL
                        ORDER BY idx DESC LIMIT 1""",
                    task_slug,
                )
                started_at = (
                    in_flight["started_at"] if in_flight else
                    (task_row["created_at"] if task_row else now)
                )

                # UPSERT task. Atributos descritivos (origin_*, complexity, impact,
                # difficulty, origin) so escrevem se nao setados ainda — evita
                # sobrescrever acidental em chamadas subsequentes.
                task_id = await conn.fetchval(
                    """INSERT INTO tasks.tasks
                        (slug, title, workflow, status, current_step, current_agent,
                         origin_stream, origin_topic,
                         complexity, impact, difficulty, origin)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                       ON CONFLICT (slug) DO UPDATE SET
                         status        = EXCLUDED.status,
                         current_step  = EXCLUDED.current_step,
                         current_agent = EXCLUDED.current_agent,
                         workflow      = COALESCE(tasks.tasks.workflow, EXCLUDED.workflow),
                         title         = COALESCE(tasks.tasks.title, EXCLUDED.title),
                         origin_stream = COALESCE(tasks.tasks.origin_stream, EXCLUDED.origin_stream),
                         origin_topic  = COALESCE(tasks.tasks.origin_topic, EXCLUDED.origin_topic),
                         complexity    = COALESCE(tasks.tasks.complexity, EXCLUDED.complexity),
                         impact        = COALESCE(tasks.tasks.impact, EXCLUDED.impact),
                         difficulty    = COALESCE(tasks.tasks.difficulty, EXCLUDED.difficulty),
                         origin        = COALESCE(tasks.tasks.origin, EXCLUDED.origin),
                         blocked_reason = CASE
                           WHEN EXCLUDED.status IN ('blocked','human_review') THEN EXCLUDED.current_step
                           ELSE NULL
                         END
                       RETURNING id""",
                    task_slug,
                    title or (task_row["title"] if task_row else task_slug),
                    wf_name,
                    (
                        "done" if next_ == "done" else
                        "blocked" if next_ == "halt" else
                        "human_review" if next_ == "human_review" else
                        "in_progress"
                    ),
                    None if is_terminal else next_,
                    None if is_terminal else resolved_agent,
                    origin_stream,
                    origin_topic,
                    complexity,
                    impact,
                    difficulty,
                    origin,
                )

                # baseline: merge no metadata_extra.baseline (dict repo->sha).
                # Aditivo: novos repos se somam; NAO sobrescreve sha existente
                # (garantia de D-analista: "baseline imutavel por repo").
                if baseline:
                    for repo, sha in baseline.items():
                        if not (repo and sha):
                            continue
                        await conn.execute(
                            """UPDATE tasks.tasks
                                  SET metadata_extra = jsonb_set(
                                        COALESCE(metadata_extra, '{}'::jsonb),
                                        ARRAY['baseline', $2],
                                        to_jsonb($3::text),
                                        true
                                      )
                                WHERE id = $1
                                  AND COALESCE(metadata_extra->'baseline'->>$2, '') = ''""",
                            task_id, repo, sha,
                        )

                # blocked_reason detalhado (CASE acima usa current_step como placeholder).
                if is_terminal and next_ == "halt":
                    await conn.execute(
                        "UPDATE tasks.tasks SET blocked_reason = $2 WHERE id = $1",
                        task_id, f"halt por {agent_name} na step '{current_step}'",
                    )
                elif is_terminal and next_ == "human_review":
                    await conn.execute(
                        "UPDATE tasks.tasks SET blocked_reason = $2 WHERE id = $1",
                        task_id, f"human_review pedido por {agent_name} na step '{current_step}'",
                    )

                # D-102: terminal `done` -> backlog item linkado vai pra
                # 'concluido'. Usuario pediu coluna separada no kanban pra
                # distinguir o que ja foi entregue do que ainda esta rodando
                # (antes ambos ficavam em 'promovido'). NOOP se task nao
                # nasceu de backlog (promoted_task_slug NULL ou row ausente).
                if is_terminal and next_ == "done":
                    await conn.execute(
                        """UPDATE tasks.backlog
                              SET status = 'concluido', updated_at = now()
                            WHERE promoted_task_slug = $1
                              AND status = 'promovido'""",
                        task_slug,
                    )

                # Fecha a fase in-flight se existe; senao cria uma fase nova ja
                # concluida (atomicamente representa a conclusao + arquivamento).
                if in_flight is not None:
                    await conn.execute(
                        """UPDATE tasks.phases
                              SET completed_at = $2, artifact = $3, summary = $4,
                                  agent = COALESCE(agent, $5), step = $6
                            WHERE id = $1""",
                        in_flight["id"], now, artifact, summary, agent_name, current_step,
                    )
                    last_idx = in_flight["idx"]
                else:
                    # Append apos o ultimo idx existente (-1 quando vazio,
                    # via COALESCE; +1 da 0 na primeira fase).
                    # NOTA: nao use `int(x or -1)` aqui — `0 or -1 == -1` em
                    # Python (0 eh falsy), o que reinserta idx=0 em tasks
                    # com exatamente uma fase e quebra o unique constraint.
                    last_idx_row = await conn.fetchval(
                        "SELECT COALESCE(MAX(idx), -1) FROM tasks.phases WHERE task_id = $1",
                        task_id,
                    )
                    last_idx = int(last_idx_row) + 1
                    await conn.execute(
                        """INSERT INTO tasks.phases
                            (task_id, idx, step, agent, started_at, completed_at, artifact, summary)
                           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
                        task_id, last_idx, current_step, agent_name, started_at, now, artifact, summary,
                    )

                # Cria fase in-flight pra proxima step (se nao terminal).
                if not is_terminal:
                    await conn.execute(
                        """INSERT INTO tasks.phases
                            (task_id, idx, step, agent, started_at)
                           VALUES ($1, $2, $3, $4, $5)""",
                        task_id, last_idx + 1, next_, resolved_agent, now,
                    )

                # Snapshot origem (pra reactor postar terminal la).
                task_final = await self._load_task_row(conn, task_slug)

                # Payload + event pro reactor consumir.
                next_artifact_default = self._expected_artifact_for(wf, next_) if not is_terminal else None
                resolved_origin_stream = task_final["origin_stream"] if task_final else None
                resolved_origin_topic = task_final["origin_topic"] if task_final else None
                resolved_next_topic = None if is_terminal else self._resolve_handoff_topic(
                    explicit_topic=next_topic,
                    next_agent=resolved_agent,
                    task_slug=task_slug,
                    origin_stream=resolved_origin_stream,
                    origin_topic=resolved_origin_topic,
                    standalone=bool(standalone),
                )
                # fresh_session do step destino: reactor usa pra zerar
                # claude_session_id da conv (stream, topic) antes de postar
                # o handoff. Ignorado em terminais (sem dispatch a fazer).
                next_fresh_session = False
                if not is_terminal and wf is not None:
                    next_step_def = wf.step(next_)
                    if next_step_def is not None:
                        next_fresh_session = next_step_def.fresh_session
                payload = {
                    "task_slug": task_slug,
                    "from_step": current_step,
                    "from_agent": agent_name,
                    "artifact": artifact,
                    "summary": summary,
                    "next": next_,
                    "next_agent": resolved_agent,
                    "next_topic": resolved_next_topic,
                    "next_artifact": next_artifact_default,
                    "next_fresh_session": next_fresh_session,
                    "standalone": bool(standalone),
                    "origin_stream": resolved_origin_stream,
                    "origin_topic":  resolved_origin_topic,
                    "workflow": task_final["workflow"] if task_final else None,
                }
                event_id = await conn.fetchval(
                    """INSERT INTO orchestrator.events (emitted_by, event_type, task_slug, payload)
                       VALUES ($1, 'phase_complete', $2, $3::jsonb)
                       RETURNING id""",
                    agent_name, task_slug, json.dumps(payload),
                )

        log.info(
            "workflow.complete_phase",
            task_slug=task_slug,
            from_step=current_step,
            from_agent=agent_name,
            artifact=artifact,
            next=next_,
            next_agent=resolved_agent,
            event_id=event_id,
            is_first_call=is_first_call,
        )

        if is_terminal:
            guidance = {
                "done": (
                    "Task closed successfully. The human will be notified via "
                    "TERMINAL_NOTIFY_STREAM and also in the origin conversation "
                    "(if registered). If you want to post an extra summary in "
                    "the current conversation, use notify_human."
                ),
                "halt": (
                    "Task paused (status=blocked). The human needs to unblock. "
                    "Use notify_human or ask_human to signal the reason."
                ),
                "human_review": (
                    "Task escalated to human (status=human_review). Use "
                    "ask_human now if the review requires a blocking answer."
                ),
            }[next_]
        else:
            guidance = (
                f"Phase '{current_step}' recorded. Handoff to '{resolved_agent}' "
                f"(step '{next_}') dispatched — the reactor will post the request at "
                f"#{resolved_agent}/{payload['next_topic']}. You do NOT need to post "
                "a separate message announcing the dispatch; it's already done. "
                "If you want to notify the human of the status, use notify_human."
            )
            # Aviso extra quando o retorno cai no topic de origem (humano esta
            # vendo essa conversa ao vivo). Sem `ask_human` / `complete_phase` a
            # run termina silenciosa mas a mensagem ja esta na thread aberta —
            # ainda assim, convem o agente bloquear corretamente.
            if (
                resolved_origin_stream
                and resolved_origin_topic
                and resolved_agent == resolved_origin_stream
                and payload["next_topic"] == resolved_origin_topic
            ):
                guidance += (
                    " This handoff returns to the human's origin topic "
                    f"(#{resolved_origin_stream}/{resolved_origin_topic}); "
                    "if you need their answer before proceeding, use "
                    "`ask_human(blocking=true)` explicitly."
                )

        return {
            "event_id": event_id,
            "task_slug": task_slug,
            "artifact_path": str(artifact_path),
            "from_step": current_step,
            "next": next_,
            "next_step": None if is_terminal else next_,
            "next_agent": resolved_agent,
            "next_artifact": next_artifact_default,
            "status": (
                "done" if next_ == "done" else
                "blocked" if next_ == "halt" else
                "human_review" if next_ == "human_review" else
                "in_progress"
            ),
            "guidance": guidance,
        }

    async def create_worktree(
        self,
        *,
        task_slug: str,
        repo: str,
        baseline_sha: str | None = None,
        branch: str | None = None,
        agent_name: str,
    ) -> dict[str, Any]:
        """Cria worktree isolada + registra no banco, atomico.

        Resolve baseline_sha quando nao fornecido (default-branch HEAD do
        remote). Cria em `${WORKTREES_DIR}/<repo>/<slug>/`, fora da arvore
        `repos/<repo>/` — worktrees nunca poluem o status do repo canonico.

        Idempotente: se (task_id, repo, branch) ja existe no banco E path em
        disco casa com um worktree valido, retorna sem recriar. Se path sumiu
        ou HEAD nao bate, recria.

        Baseline resolution:
          - explicit `baseline_sha` sempre ganha prioridade;
          - senao, `git fetch --quiet origin` + `git rev-parse origin/<default>`,
            onde `<default>` vem de `git symbolic-ref refs/remotes/origin/HEAD`.
        """
        if not SLUG_RE.match(task_slug):
            raise WorkflowError(f"invalid task_slug: {task_slug!r}")
        if not repo:
            raise WorkflowError("repo is required.")

        repos_root = Path(os.environ.get("WORKSPACE_REPOS", "/workspace/repos"))
        worktrees_root = Path(os.environ.get("WORKTREES_DIR", "/workspace/worktrees"))
        repo_dir = repos_root / repo
        if not (repo_dir / ".git").exists():
            raise WorkflowError(
                f"Repo {repo!r} does not exist in {repos_root}/ (looked for: {repo_dir}/.git). "
                "Check that the repo is cloned in instance/repos/ and that the mount "
                "reaches the container."
            )

        branch_final = branch or f"task/{task_slug}"
        wt_path = worktrees_root / repo / task_slug

        async with self._pool.acquire() as conn:
            task_id = await conn.fetchval(
                "SELECT id FROM tasks.tasks WHERE slug = $1", task_slug,
            )
            if task_id is None:
                raise WorkflowError(
                    f"Task {task_slug!r} does not exist. Run complete_phase first to create it."
                )

        # Resolve baseline antes de mexer em disco — se falhar, nada foi alterado.
        if not baseline_sha:
            baseline_sha = await self._resolve_default_baseline(repo_dir)
        elif not re.fullmatch(r"[0-9a-f]{7,40}", baseline_sha):
            raise WorkflowError(
                f"baseline_sha {baseline_sha!r} does not look like a hex SHA (7-40 chars)."
            )

        # Idempotencia: ja existe worktree valida nesse path apontando pro branch?
        existing = await self._existing_worktree(repo_dir, wt_path, branch_final)
        if existing is None:
            # Cria. Se algo no caminho ficou sujo (path existe sem ser worktree,
            # ou branch ja existe solta), aborta com msg — nao tentamos chute.
            worktrees_root.mkdir(parents=True, exist_ok=True)
            (worktrees_root / repo).mkdir(parents=True, exist_ok=True)
            await self._git_worktree_add(repo_dir, wt_path, branch_final, baseline_sha)

        wt_path_str = str(wt_path)
        async with self._pool.acquire() as conn:
            # Upsert por (task_id, repo, branch). Path atualiza se mudou.
            await conn.execute(
                """INSERT INTO tasks.worktrees (task_id, repo, branch, path)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (task_id, repo, branch) DO UPDATE SET path = EXCLUDED.path""",
                task_id, repo, branch_final, wt_path_str,
            )
            # Guarda baseline_sha no metadata_extra (append por repo).
            await conn.execute(
                """UPDATE tasks.tasks
                      SET metadata_extra = jsonb_set(
                            COALESCE(metadata_extra, '{}'::jsonb),
                            ARRAY['baseline', $2],
                            to_jsonb($3::text),
                            true
                          )
                    WHERE id = $1""",
                task_id, repo, baseline_sha,
            )
        log.info(
            "workflow.create_worktree",
            task_slug=task_slug, repo=repo, path=wt_path_str, branch=branch_final,
            baseline_sha=baseline_sha, reused=existing is not None, agent=agent_name,
        )
        guidance = (
            f"Worktree ready at {wt_path_str}. `cd {wt_path_str}` and work "
            "from there — do not edit /workspace/repos/<repo>/ directly. All "
            "commits must come from branch '" + branch_final + "'."
        )
        if existing is not None:
            guidance = "Existing worktree reused. " + guidance
        return {
            "task_slug": task_slug,
            "worktree": {
                "repo": repo, "branch": branch_final, "path": wt_path_str,
                "baseline_sha": baseline_sha, "created_by": agent_name,
                "reused": existing is not None,
            },
            "guidance": guidance,
        }

    async def _resolve_default_baseline(self, repo_dir: Path) -> str:
        """Resolve SHA do default branch remoto.

        Fluxo:
          1. `git fetch --quiet origin` — garante refs atualizadas.
          2. `git symbolic-ref refs/remotes/origin/HEAD` -> `refs/remotes/origin/<default>`.
          3. `git rev-parse refs/remotes/origin/<default>` -> SHA.

        Se origin/HEAD nao estiver setada localmente (clone antigo), tenta
        `git remote set-head origin --auto` antes. Se tudo falhar, sobe erro
        explicativo — NAO chutamos `main` ou `master`.
        """
        fetch = await asyncio.create_subprocess_exec(
            "git", "-C", str(repo_dir), "fetch", "--quiet", "origin",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await fetch.communicate()
        if fetch.returncode != 0:
            raise WorkflowError(
                f"git fetch failed at {repo_dir}: "
                + stderr.decode("utf-8", "replace").strip()
            )

        head_ref = await self._git_symbolic_ref_head(repo_dir)
        if head_ref is None:
            # Tenta auto-detectar e setar origin/HEAD localmente.
            auto = await asyncio.create_subprocess_exec(
                "git", "-C", str(repo_dir), "remote", "set-head", "origin", "--auto",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await auto.communicate()
            head_ref = await self._git_symbolic_ref_head(repo_dir)
        if head_ref is None:
            raise WorkflowError(
                f"Could not resolve default branch at {repo_dir} "
                "(`origin/HEAD` missing). Pass an explicit baseline_sha."
            )

        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(repo_dir), "rev-parse", head_ref,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise WorkflowError(
                f"rev-parse {head_ref} failed: "
                + stderr.decode("utf-8", "replace").strip()
            )
        sha = stdout.decode("utf-8", "replace").strip()
        if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
            raise WorkflowError(f"rev-parse returned invalid SHA: {sha!r}")
        return sha

    async def _git_symbolic_ref_head(self, repo_dir: Path) -> str | None:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(repo_dir),
            "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        ref = stdout.decode("utf-8", "replace").strip()
        return ref or None

    async def _existing_worktree(
        self, repo_dir: Path, wt_path: Path, branch: str,
    ) -> dict[str, Any] | None:
        """Checa se ja existe worktree valida no path + branch esperados.

        Retorna dict com info quando casa; None caso contrario.
        """
        if not wt_path.exists():
            return None
        # `git worktree list --porcelain` — parseamos pra achar entry com esse path.
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(repo_dir), "worktree", "list", "--porcelain",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        entries: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in stdout.decode("utf-8", "replace").splitlines():
            if not line.strip():
                if current:
                    entries.append(current); current = {}
                continue
            k, _, v = line.partition(" ")
            current[k] = v
        if current:
            entries.append(current)
        wt_abs = str(wt_path.resolve()) if wt_path.exists() else str(wt_path)
        for e in entries:
            if e.get("worktree") == wt_abs or e.get("worktree") == str(wt_path):
                ref = e.get("branch", "")
                expected = f"refs/heads/{branch}"
                if ref == expected:
                    return {"path": e["worktree"], "branch": branch}
                # Path existe mas branch diferente — retorna None pra forcar erro
                # no git worktree add (que falhara com "already exists").
                return None
        return None

    async def _git_worktree_add(
        self, repo_dir: Path, wt_path: Path, branch: str, baseline_sha: str,
    ) -> None:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(repo_dir),
            "worktree", "add", "-b", branch, str(wt_path), baseline_sha,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", "replace").strip()
            out = stdout.decode("utf-8", "replace").strip()
            raise WorkflowError(
                f"git worktree add failed (rc={proc.returncode}): {err or out}"
            )

    async def cleanup_worktrees(
        self,
        *,
        task_slug: str,
        agent_name: str,
    ) -> dict[str, Any]:
        """Remove todas as worktrees registradas pra uma task.

        Fluxo por worktree:
          1. `git -C <repo_canonico> worktree remove --force <path>` — remove
             registro do git + apaga pasta (quando path ainda existe em disco).
          2. `git -C <repo_canonico> worktree prune` — garante que registros
             orfaos no git sumam mesmo se a pasta ja nao existia.
          3. `shutil.rmtree(path)` como backstop — se por algum motivo o
             worktree remove falhou mas a pasta persistiu.
          4. `DELETE FROM tasks.worktrees WHERE ...` — so apaga a linha se o
             path realmente sumiu do disco.

        Idempotente: rodar de novo em task ja limpa nao causa erro; retorna
        removed=[] e guidance informando.

        Falhas nao sao mascaradas: se alguma worktree nao pode ser removida,
        retorna em `failed` e o chamador (tipicamente coordenador no
        encerramento) deve escalar via `complete_phase(next='human_review')`.

        Repo canonico e resolvido como `/workspace/repos/<repo>` — convencao
        de bind mount do framework. Se a instancia usar prefixo diferente,
        esta logica precisa mudar junto.
        """
        if not SLUG_RE.match(task_slug):
            raise WorkflowError(f"invalid task_slug: {task_slug!r}")

        async with self._pool.acquire() as conn:
            task_id = await conn.fetchval(
                "SELECT id FROM tasks.tasks WHERE slug = $1", task_slug,
            )
            if task_id is None:
                raise WorkflowError(f"Task {task_slug!r} does not exist.")
            rows = await conn.fetch(
                "SELECT repo, branch, path FROM tasks.worktrees WHERE task_id = $1",
                task_id,
            )

        if not rows:
            log.info(
                "workflow.cleanup_worktrees.noop",
                task_slug=task_slug, agent=agent_name,
            )
            return {
                "task_slug": task_slug,
                "removed": [],
                "failed": [],
                "guidance": (
                    "No worktrees registered for this task — nothing to clean up. "
                    "You may proceed."
                ),
            }

        removed: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []

        for r in rows:
            repo = r["repo"]
            path = r["path"]
            branch = r["branch"]
            canonical_repo = f"/workspace/repos/{repo}"
            errors: list[str] = []

            # 1) git worktree remove --force
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git", "-C", canonical_repo,
                    "worktree", "remove", "--force", path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode != 0:
                    errors.append(
                        f"worktree remove rc={proc.returncode}: "
                        + stderr.decode("utf-8", "replace").strip()
                    )
            except FileNotFoundError:
                errors.append("git binary missing in the container")
            except Exception as e:  # noqa: BLE001
                errors.append(f"worktree remove exc: {e}")

            # 2) git worktree prune (limpa registros orfaos)
            try:
                prune_proc = await asyncio.create_subprocess_exec(
                    "git", "-C", canonical_repo, "worktree", "prune",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await prune_proc.communicate()
            except Exception as e:  # noqa: BLE001
                errors.append(f"worktree prune exc: {e}")

            # 3) rmtree backstop — se path persiste, remove manualmente
            if os.path.exists(path):
                try:
                    shutil.rmtree(path)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"rmtree {path} failed: {e}")

            if not os.path.exists(path):
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        """DELETE FROM tasks.worktrees
                            WHERE task_id = $1 AND repo = $2 AND branch = $3""",
                        task_id, repo, branch,
                    )
                removed.append({
                    "repo": repo, "path": path, "branch": branch,
                    "warnings": errors or None,
                })
            else:
                failed.append({
                    "repo": repo, "path": path, "branch": branch,
                    "errors": errors or ["path persists after remove+prune+rmtree"],
                })

        log.info(
            "workflow.cleanup_worktrees",
            task_slug=task_slug, agent=agent_name,
            removed_count=len(removed), failed_count=len(failed),
        )

        if failed:
            guidance = (
                f"Partial cleanup: {len(removed)} removed, {len(failed)} "
                "failed. Do NOT close the task — investigate the errors, and if "
                "you can't resolve them, escalate via complete_phase(next='human_review')."
            )
        else:
            guidance = (
                f"Cleanup complete: {len(removed)} worktree(s) removed and "
                "deleted from tasks.worktrees. You may call "
                "complete_phase(next='done')."
            )

        return {
            "task_slug": task_slug,
            "removed": removed,
            "failed": failed,
            "guidance": guidance,
        }

    async def get_task_state(self, task_slug: str) -> dict[str, Any]:
        if not SLUG_RE.match(task_slug):
            raise WorkflowError(f"invalid task_slug: {task_slug!r}")
        async with self._pool.acquire() as conn:
            row = await self._load_task_row(conn, task_slug)
            if row is None:
                raise WorkflowError(f"Task {task_slug!r} does not exist.")
            phases = await self._load_phases(conn, row["id"])
            worktrees = await self._load_worktrees(conn, row["id"])
        extra = row.get("metadata_extra") or {}
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except Exception:
                extra = {}
        return {
            "slug": row["slug"],
            "title": row["title"],
            "workflow": row["workflow"],
            "status": row["status"],
            "current_step": row["current_step"],
            "current_agent": row["current_agent"],
            "complexity": row["complexity"],
            "baseline": extra.get("baseline") or {},
            "worktrees": worktrees,
            "origin": {
                "stream": row["origin_stream"],
                "topic": row["origin_topic"],
            },
            "phases_done": [
                {"step": p["step"], "agent": p["agent"], "artifact": p["artifact"]}
                for p in phases if p["completed_at"] is not None
            ],
            "blocked_reason": row["blocked_reason"],
        }

    async def reopen_task(
        self,
        *,
        task_slug: str,
        agent_name: str,
        next_step: str,
        next_agent: str | None = None,
        reason: str,
        standalone: bool = False,
    ) -> dict[str, Any]:
        """D-57 fase 2.5: reabre task em status terminal pra rodar mais um step.

        Uso esperado: humano pede revisao pos-encerramento ("vi a task em done,
        falta X"). Coordenador (ou outro agente instruido explicitamente) chama
        reopen_task pra voltar a task pra in_progress no step desejado. O
        reactor recebe um evento phase_complete normal e despacha pro
        next_agent — mesmo fluxo de complete_phase.

        Difere de complete_phase em dois pontos:
          - Nao exige artifact (nenhuma fase foi concluida agora; so estamos
            religando a task).
          - Aceita transicionar *pra* um step do workflow sem passar por
            complete_phase do step anterior (porque o estado terminal ja foi
            registrado no fechamento original).

        Nao muda o prompt de nenhum agente — por design (ver D-57). Fica como
        ferramenta disponivel, so invocada quando humano pedir explicitamente.
        """
        if not SLUG_RE.match(task_slug):
            raise WorkflowError(f"invalid task_slug: {task_slug!r}")
        if not next_step:
            raise WorkflowError("next_step is required")
        if not reason or not reason.strip():
            raise WorkflowError("reason is required — recorded in blocked_reason for auditing")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                task_row = await self._load_task_row(conn, task_slug)
                if task_row is None:
                    raise WorkflowError(f"Task {task_slug!r} does not exist.")
                current_status = task_row["status"]
                if current_status not in ("done", "blocked", "human_review"):
                    raise WorkflowError(
                        f"Task {task_slug!r} is not in a terminal status (current: {current_status!r}). "
                        f"reopen_task only makes sense to reopen a closed task. For normal progress use complete_phase."
                    )

                wf = self._resolve_workflow(task_row.get("workflow"))
                if wf is None:
                    raise WorkflowError(
                        f"Task {task_slug!r} has no declared workflow — cannot validate next_step."
                    )
                if next_step in TERMINALS:
                    raise WorkflowError(
                        f"next_step cannot be terminal ({next_step}). reopen_task reopens the task to a working step."
                    )
                step_def = wf.step(next_step)
                if step_def is None:
                    raise WorkflowError(
                        f"step {next_step!r} does not exist in workflow {wf.name!r}. "
                        f"Valid steps: {sorted(wf.steps.keys())}."
                    )
                resolved_agent = next_agent or step_def.agent
                if not resolved_agent:
                    raise WorkflowError(
                        f"step {next_step!r} has no default agent; pass an explicit next_agent."
                    )

                now = datetime.now(tz=timezone.utc)
                task_id = task_row["id"]

                # Religa a task: status volta pra in_progress, current_step/agent
                # apontam pro alvo, blocked_reason mantido historicamente no
                # payload do evento (mas removido da row — ela nao esta mais
                # bloqueada).
                await conn.execute(
                    """UPDATE tasks.tasks
                          SET status = 'in_progress',
                              current_step = $2,
                              current_agent = $3,
                              blocked_reason = NULL,
                              updated_at = now()
                        WHERE id = $1""",
                    task_id, next_step, resolved_agent,
                )

                # Cria fase in-flight pro next_step (igual a complete_phase faz
                # no branch nao-terminal).
                last_idx_row = await conn.fetchval(
                    "SELECT COALESCE(MAX(idx), -1) FROM tasks.phases WHERE task_id = $1",
                    task_id,
                )
                # COALESCE retorna -1 pra task vazia; +1 da 0. Nao usar
                # `int(x or -1)` — `0 or -1 == -1` em Python falsifica o caso
                # MAX(idx)=0 (task com exatamente uma fase) e gera duplicate.
                next_idx = int(last_idx_row) + 1
                await conn.execute(
                    """INSERT INTO tasks.phases
                        (task_id, idx, step, agent, started_at)
                       VALUES ($1, $2, $3, $4, $5)""",
                    task_id, next_idx, next_step, resolved_agent, now,
                )

                # Evento phase_complete pro reactor despachar. from_step aponta
                # pro ultimo step completado antes do terminal — leitura
                # informativa, nao controla validacao.
                last_done = await conn.fetchrow(
                    """SELECT step FROM tasks.phases
                        WHERE task_id = $1 AND completed_at IS NOT NULL
                        ORDER BY idx DESC LIMIT 1""",
                    task_id,
                )
                from_step = last_done["step"] if last_done else (task_row.get("current_step") or "?")

                next_artifact_default = self._expected_artifact_for(wf, next_step)
                resolved_origin_stream = task_row.get("origin_stream")
                resolved_origin_topic = task_row.get("origin_topic")
                resolved_next_topic = self._resolve_handoff_topic(
                    explicit_topic=None,
                    next_agent=resolved_agent,
                    task_slug=task_slug,
                    origin_stream=resolved_origin_stream,
                    origin_topic=resolved_origin_topic,
                    standalone=bool(standalone),
                )
                next_step_def = wf.step(next_step) if wf else None
                payload = {
                    "task_slug": task_slug,
                    "from_step": from_step,
                    "from_agent": agent_name,
                    "artifact": f"reopen:{reason[:100]}",
                    "summary": reason,
                    "next": next_step,
                    "next_agent": resolved_agent,
                    "next_topic": resolved_next_topic,
                    "next_artifact": next_artifact_default,
                    "next_fresh_session": (
                        next_step_def.fresh_session if next_step_def else False
                    ),
                    "origin_stream": resolved_origin_stream,
                    "origin_topic": resolved_origin_topic,
                    "workflow": task_row.get("workflow"),
                    "reopen": True,
                    "prev_status": current_status,
                    "standalone": bool(standalone),
                }
                event_id = await conn.fetchval(
                    """INSERT INTO orchestrator.events (emitted_by, event_type, task_slug, payload)
                       VALUES ($1, 'phase_complete', $2, $3::jsonb)
                       RETURNING id""",
                    agent_name, task_slug, json.dumps(payload),
                )

        log.info(
            "workflow.reopen_task",
            task_slug=task_slug,
            prev_status=current_status,
            next_step=next_step,
            next_agent=resolved_agent,
            reason=reason[:100],
            event_id=event_id,
        )

        return {
            "event_id": event_id,
            "task_slug": task_slug,
            "prev_status": current_status,
            "next_step": next_step,
            "next_agent": resolved_agent,
            "next_artifact": next_artifact_default,
            "status": "in_progress",
            "guidance": (
                f"Task {task_slug!r} reopened — status is back to 'in_progress' at step '{next_step}', "
                f"the reactor will post the request at #{resolved_agent}/{resolved_next_topic}. "
                f"Reason recorded in the event: {reason[:200]}"
            ),
        }
