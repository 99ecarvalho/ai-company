"""Orchestrator reactor — consome orchestrator.events e posta handoffs via broker.

- LISTEN event_new no Postgres (trigger dispara em cada INSERT pending).
- Pra cada evento pending:
    next in {done, halt, human_review}: posta em #TERMINAL_NOTIFY_STREAM
    next = nome de agente: posta handoff em #<agente> no topic definido
- Marca status processed (ou quarantined + attempts+=1 em erro).
- Polling fallback a cada POLL_INTERVAL_SEC (caso LISTEN caia).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import time

import aiohttp
import asyncpg
import structlog

from .health import start_health_server


DATABASE_URL = os.environ["DATABASE_URL"]
BROKER_URL = os.environ["BROKER_URL"].rstrip("/")
BROKER_TOKEN = os.environ["BROKER_TOKEN"]
POLL_INTERVAL_SEC = float(os.environ.get("POLL_INTERVAL_SEC", "2.0"))
QUARANTINE_MAX_RETRIES = int(os.environ.get("QUARANTINE_MAX_RETRIES", "3"))
# Stream agregador de terminais. Se vazio, reactor posta so na conversa de
# origem. Instancias configuram o nome do stream via env (ex: 'orquestracao',
# 'ops', 'tasks-feed', etc).
TERMINAL_NOTIFY_STREAM = os.environ.get("TERMINAL_NOTIFY_STREAM", "").strip()
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "8810"))

# health stats
_stats = {"started_at": time.time(), "events_processed": 0, "last_event_at": None}

logging_level = os.environ.get("LOG_LEVEL", "INFO").upper()
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, logging_level, 20)),
    logger_factory=structlog.PrintLoggerFactory(),
)
structlog.contextvars.bind_contextvars(component="orchestrator-reactor")
log = structlog.get_logger("reactor")


async def post_message(
    http: aiohttp.ClientSession,
    stream: str,
    topic: str,
    content: str,
    parent_conv_id: int | None = None,
) -> None:
    """D-87: `parent_conv_id` persiste em messaging.conversations quando
    cria a conv (primeira msg). Handoffs de complete_phase passam o
    conv_id da origem como parent — hierarquia via FK, sem heuristica."""
    body: dict = {"stream": stream, "topic": topic, "content": content}
    if parent_conv_id is not None:
        body["parent_conv_id"] = parent_conv_id
    async with http.post(
        f"{BROKER_URL}/api/messages",
        json=body,
    ) as r:
        if r.status >= 400:
            body_err = await r.text()
            raise RuntimeError(f"post failed HTTP {r.status}: {body_err[:200]}")


def _handoff_body(ev_payload: dict) -> str:
    slug = ev_payload.get("task_slug")
    next_artifact = ev_payload.get("next_artifact")
    # Promocao do backlog: payload carrega `backlog_slug` + `backlog_content`.
    # Formato diferente — o humano/agente precisa ver o corpo do item como
    # primeira mensagem, senao o contexto original do backlog fica invisivel.
    # Nao referenciamos `company/tasks/<slug>/` porque o diretorio ainda nao
    # existe no FS no momento da promocao (so ha linha no DB).
    if ev_payload.get("backlog_slug"):
        title = ev_payload.get("summary", "—").replace("promoted from backlog: ", "", 1)
        body = (ev_payload.get("backlog_content") or "").strip()
        lines = [
            f"📋 **Promoted from backlog** — `{ev_payload.get('backlog_slug')}`",
            "",
            f"**Task:** `{slug}` — {title}",
            f"**Promoted by:** `{ev_payload.get('from_agent')}`",
            "",
            "---",
            "",
            body if body else "_(backlog item with no content)_",
            "",
            "---",
            "",
            f"_When done, call `complete_phase(task_slug='{slug}', "
            f"artifact='{next_artifact or '<your-artifact>'}', summary=..., next=...)`._",
        ]
        return "\n".join(lines)
    # Normal handoff (phase -> phase).
    from_step = ev_payload.get("from_step") or ev_payload.get("from_phase")
    next_step = ev_payload.get("next")
    lines = [
        f"➡️ **Handoff from `{ev_payload.get('from_agent')}`**",
        "",
        f"**Task:** `{slug}` — you take over phase **{next_step}**.",
        f"**Previous step:** `{from_step}` (artifact: `{ev_payload.get('artifact')}`)",
        "",
        f"**Summary:** {ev_payload.get('summary', '—')}",
        "",
        f"_Context in `company/tasks/{slug}/`. Use `get_task_state(task_slug='{slug}')` to read structured state. "
        f"When done, call `complete_phase(task_slug='{slug}', artifact='{next_artifact or '<your-artifact>'}', summary=..., next=...)`._",
    ]
    return "\n".join(lines)


def _terminal_body(ev_payload: dict) -> str:
    slug = ev_payload.get("task_slug")
    next_ = ev_payload.get("next")
    flag = {
        "done": "🏁",
        "halt": "⏸️",
        "human_review": "🙋",
    }.get(next_, "❓")
    label = {
        "done": "closed successfully",
        "halt": "paused — human needs to unblock",
        "human_review": "escalated for human review",
    }.get(next_, next_ or "—")
    return (
        f"{flag} **Task `{slug}` {label}**\n\n"
        f"**Summary:** {ev_payload.get('summary', '—')}\n"
        f"**Origin:** `{ev_payload.get('from_agent')}` / step `{ev_payload.get('from_step')}` / "
        f"artifact `{ev_payload.get('artifact')}`"
    )


async def _resolve_conv_id(
    pool: asyncpg.Pool, stream: str | None, topic: str | None
) -> int | None:
    """D-87: resolve conversation_id via (stream, topic). Usado pra setar
    parent_conv_id na conv filha criada por handoff/terminal de complete_phase."""
    if not stream or not topic:
        return None
    row = await pool.fetchrow(
        """SELECT c.id
             FROM messaging.conversations c
             JOIN messaging.streams s ON s.id = c.stream_id
            WHERE s.name = $1 AND c.topic_name = $2
            LIMIT 1""",
        stream, topic,
    )
    return row["id"] if row else None


async def _process_one(http: aiohttp.ClientSession, pool: asyncpg.Pool, event_id: int) -> None:
    """Marca processing, posta, marca processed (ou quarantined)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """UPDATE orchestrator.events SET status = 'processing'
                   WHERE id = $1 AND status = 'pending' RETURNING id, payload, attempts""",
                event_id,
            )
            if row is None:
                return  # ja processado por outro worker
    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    try:
        next_ = payload.get("next")
        task_slug = payload.get("task_slug")
        # Guard pos-terminal: ignora handoff stale se a task ja esta em
        # status terminal (`done`/`concluido`/`descartado`). Pode acontecer
        # quando filho `complete_phase` chega tarde (ex: depois do PO ter
        # encerrado a task num caminho paralelo). Terminal events em si
        # (next in done/halt/human_review) ignoram este guard — eles
        # estao **declarando** o terminal, nao reagindo a um.
        if task_slug and next_ not in ("done", "halt", "human_review"):
            t_status = await pool.fetchval(
                "SELECT status FROM tasks.tasks WHERE slug = $1",
                task_slug,
            )
            if t_status in ("done", "blocked", "human_review"):
                # Marca o evento como processado pra remover da fila sem
                # postar nada. last_error registra a razao pra auditoria.
                await pool.execute(
                    """UPDATE orchestrator.events
                          SET status = 'processed', processed_at = now(),
                              last_error = $2
                        WHERE id = $1""",
                    event_id, f"dropped: task already in terminal status {t_status!r}",
                )
                log.info(
                    "reactor.event_dropped_post_terminal",
                    event_id=event_id, task_slug=task_slug,
                    task_status=t_status, payload_next=next_,
                )
                return
        if next_ in ("done", "halt", "human_review"):
            # Terminal: status ja persistido pelo WorkflowManager; aqui so
            # notificamos. Dois destinos, ambos best-effort (falha de post
            # loga warning, nao derruba o evento):
            #   (1) TERMINAL_NOTIFY_STREAM (configuravel via env) — feed
            #       agregador de todos os terminais da empresa.
            #   (2) conversa de origem (origin_stream/origin_topic em
            #       metadata) — onde o humano pediu a task; e la que ele
            #       vai perceber que acabou sem ter que trocar de tab.
            terminal_body = _terminal_body(payload)
            origin_stream = payload.get("origin_stream")
            origin_topic = payload.get("origin_topic")
            # D-87: origin eh a conv que originou a task — vira pai de
            # ambas (terminal agregador + reply no origin) pra cascata visual.
            origin_conv_id = await _resolve_conv_id(pool, origin_stream, origin_topic)
            if TERMINAL_NOTIFY_STREAM:
                try:
                    await post_message(
                        http, TERMINAL_NOTIFY_STREAM, f"task-{task_slug}-final",
                        terminal_body,
                        parent_conv_id=origin_conv_id,
                    )
                except Exception as post_exc:
                    log.warning(
                        "reactor.terminal_notify_failed",
                        event_id=event_id, task_slug=task_slug, next=next_,
                        stream=TERMINAL_NOTIFY_STREAM,
                        error=str(post_exc)[:300],
                    )
            if origin_stream and origin_topic:
                # Skip echo no proprio agente: se quem completou a fase final
                # eh o dono do origin_stream, postar terminal_body na mesma
                # conv acaba acordando o agente de novo (mensagem entra no
                # inbox dele), gerando uma cascade de runs redundantes pos-
                # `done`. Sintoma observado no pilot ops/2026-05-05:
                # 3 runs apos `done` consumindo ~$0.20 / 7 turns por task,
                # gerando msgs tipo "Task encerrada", "Mensagem atrasada do
                # reactor", etc. No multi-agente classico isso nao aparecia
                # porque executor (from_agent) e PO (origin_stream) sao
                # diferentes — o post no origin acordava o PO, que era o
                # comportamento desejado. Aqui mantemos esse comportamento:
                # so pulamos quando from_agent == origin_stream. O
                # TERMINAL_NOTIFY_STREAM post (acima) continua acontecendo
                # — feed agregador pro humano.
                from_agent = payload.get("from_agent")
                if from_agent and from_agent == origin_stream:
                    log.info(
                        "reactor.terminal_origin_skipped_self",
                        event_id=event_id, task_slug=task_slug,
                        from_agent=from_agent, origin_stream=origin_stream,
                    )
                else:
                    try:
                        # Mesmo origin_stream+origin_topic — conv ja existe, o
                        # parent_conv_id e ignorado pelo broker (ON CONFLICT path).
                        await post_message(http, origin_stream, origin_topic, terminal_body)
                    except Exception as post_exc:
                        log.warning(
                            "reactor.origin_notify_failed",
                            event_id=event_id, task_slug=task_slug,
                            stream=origin_stream, topic=origin_topic,
                            error=str(post_exc)[:300],
                        )
        else:
            target_stream = payload.get("next_agent") or next_  # back-compat: old events had agent em 'next'
            target_topic = payload.get("next_topic") or f"task-{task_slug}"
            origin_stream = payload.get("origin_stream")
            origin_topic = payload.get("origin_topic")
            # fresh_session: workflow declarou que ao entrar neste step a janela
            # de contexto deve ser fresca. Limpa claude_session_id da conv antes
            # do post — proximo spawn de claude_runner nao usa --resume e roda
            # `claude -p` sem buffer prior. As `instructions` do step continuam
            # entrando via append-system-prompt (build dinamico em claude_runner).
            # No-op se a conv ainda nao existe (UPDATE 0 rows).
            if payload.get("next_fresh_session"):
                cleared = await pool.execute(
                    """UPDATE messaging.conversations c
                          SET claude_session_id = NULL,
                              claude_session_cwd = NULL
                         FROM messaging.streams s
                        WHERE c.stream_id = s.id
                          AND s.name = $1
                          AND c.topic_name = $2""",
                    target_stream, target_topic,
                )
                log.info(
                    "reactor.fresh_session_cleared",
                    event_id=event_id, task_slug=task_slug,
                    target_stream=target_stream, target_topic=target_topic,
                    rows=cleared,
                )
            # SEMPRE posta o handoff — inclusive quando next_agent == from_agent
            # no mesmo topic. Tentar pular como "self-handoff redundante" trava
            # a fase: o agente que chamou complete_phase encerra o turno em
            # seguida (run_end), entao a proxima fase fica orfa sem alguem
            # acordar pra executar. O design assumia "agente continua no mesmo
            # turn", mas claude_runner nao tem essa garantia — apos
            # complete_phase, o agente pode encerrar (quase sempre encerra).
            # Sintoma confirmado em 2026-04-30 na task
            # filtro-processos-internos-dashboard-assessoria: PO fez
            # complete_phase(next='encerramento') na conv-task, run_end logo
            # depois, reactor pulou o handoff, fase encerramento nunca
            # executou — task travada em current_step='encerramento'.
            #
            # O sintoma original que motivou o skip ("este handoff e um
            # evento duplicado", D-103/cb009d3) era o PO chamando
            # complete_phase numa conv humano-PO (chat livre, nao conv-task);
            # o handoff postado virava ruido visual ali. Hoje, pos-refactor
            # de prompts, PO trabalha em conv-task (`product-owner/task-X`),
            # e o handoff postado nessa conv eh exatamente o trigger pra fase
            # ser executada. Se um caso futuro repetir o D-103 (PO em conv
            # humano fazendo complete_phase), o pior cenario eh o handoff
            # virar ruido visual la — funcionalmente nao trava nada.
            origin_conv_id = await _resolve_conv_id(
                pool, origin_stream, origin_topic,
            )
            # Caminho A do `backlog_promote` (D-110): orchestrator declarado
            # pelo workflow != dispatch_agent. Cria conv-supervisora no stream
            # do orchestrator antes do handoff, com mensagem indicadora pra o
            # humano achar o ponto onde acompanhar a task. Conv da fase
            # inicial (target) fica filha da supervisora — terminal_notify
            # quando task terminar volta pra supervisora.
            #
            # Em Caminho B (orchestrator == dispatch_agent), a "supervisora"
            # eh a propria conv da primeira fase (mesma stream + mesmo topic);
            # o post_message logo abaixo cria essa conv unificada — sem
            # supervisora separada, sem mensagem extra.
            is_promote = bool(payload.get("backlog_slug"))
            if (is_promote
                    and origin_conv_id is None
                    and origin_stream
                    and origin_topic
                    and (origin_stream, origin_topic) != (target_stream, target_topic)):
                supervisor_body = (
                    f"📋 **Task promoted** — `{task_slug}`\n\n"
                    f"Orchestrator: `{origin_stream}` (you).\n"
                    f"Initial phase dispatched to `{target_stream}`.\n\n"
                    f"Track from here — terminals (`done`, `halt`, `human_review`) "
                    f"come back to this conv when the task closes."
                )
                await post_message(
                    http, origin_stream, origin_topic, supervisor_body,
                    parent_conv_id=None,  # supervisora eh root
                )
                # re-resolve agora que a conv existe
                origin_conv_id = await _resolve_conv_id(
                    pool, origin_stream, origin_topic,
                )
            # standalone=True (mega-agente fan-out): nao linka parent_conv_id,
            # conv-task fica root no stream destino. Aparece na sidebar e eh
            # conversavel pelo humano (D-96 esconde filhas + read-only). Sem
            # standalone, comportamento default mantem hierarquia (multi-agente
            # via PO/executor: humano so conversa com PO; tasks viram filhas
            # read-only no header). Vinculo logico task<->origin continua via
            # tasks.tasks.origin_stream/origin_topic — telemetry e UI da task
            # page nao perdem rastreabilidade.
            if payload.get("standalone"):
                log.info(
                    "reactor.standalone_handoff",
                    event_id=event_id, task_slug=task_slug,
                    target_stream=target_stream, target_topic=target_topic,
                )
                effective_parent = None
            else:
                effective_parent = origin_conv_id
            await post_message(
                http, target_stream, target_topic, _handoff_body(payload),
                parent_conv_id=effective_parent,
            )
        await pool.execute(
            "UPDATE orchestrator.events SET status = 'processed', processed_at = now() WHERE id = $1",
            event_id,
        )
        log.info("reactor.processed", event_id=event_id, next=next_, task_slug=task_slug)
        _stats["events_processed"] += 1
        _stats["last_event_at"] = time.time()
    except Exception as e:
        new_attempts = row["attempts"] + 1
        new_status = "pending" if new_attempts < QUARANTINE_MAX_RETRIES else "dead"
        await pool.execute(
            "UPDATE orchestrator.events SET status = $2, attempts = $3, last_error = $4 WHERE id = $1",
            event_id, new_status, new_attempts, str(e)[:500],
        )
        log.exception("reactor.process_failed", event_id=event_id, attempt=new_attempts, status=new_status)


async def _drain_pending(http: aiohttp.ClientSession, pool: asyncpg.Pool) -> None:
    """Pega pending events (ordem FIFO) e processa um a um."""
    rows = await pool.fetch(
        """SELECT id FROM orchestrator.events
             WHERE status = 'pending' AND attempts < $1
             ORDER BY created_at ASC LIMIT 50""",
        QUARANTINE_MAX_RETRIES,
    )
    for r in rows:
        await _process_one(http, pool, r["id"])


def _health_status() -> dict:
    return {
        "status": "ok",
        "uptime_sec": int(time.time() - _stats["started_at"]),
        "events_processed": _stats["events_processed"],
        "last_event_at": _stats["last_event_at"],
    }


async def main():
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4)
    start_health_server(HEALTH_PORT, _health_status)
    log.info("reactor.starting", broker=BROKER_URL, terminal_stream=TERMINAL_NOTIFY_STREAM, health_port=HEALTH_PORT)

    async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {BROKER_TOKEN}"}) as http:
        # Drain inicial
        await _drain_pending(http, pool)

        # LISTEN em conn dedicada
        listen_conn = await asyncpg.connect(DATABASE_URL)
        notify_q: asyncio.Queue = asyncio.Queue()

        def _cb(_conn, _pid, _channel, payload):
            notify_q.put_nowait(payload)

        await listen_conn.add_listener("event_new", _cb)

        while True:
            try:
                await asyncio.wait_for(notify_q.get(), timeout=POLL_INTERVAL_SEC)
            except asyncio.TimeoutError:
                pass
            await _drain_pending(http, pool)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
