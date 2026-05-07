"""FastAPI app — serve PWA + broker interno + endpoints de transcribe/push/memory/telemetry/hire.

Rotas de messaging/conversations/streams/users/asks/events estao em broker.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import asyncpg
import bcrypt
import docker
import structlog
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import auth as auth_mod
from . import db
from .auth import (
    Principal,
    SESSION_COOKIE,
    SESSION_TTL_DAYS,
    cookie_secure,
    create_session,
    delete_session,
    get_principal,
)
from .broker import router as broker_router
from .files import router as files_router
from .scheduler_routes import router as scheduler_routes_router
from .push_dispatcher import PushDispatcher


# ---------- Logging ----------

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    logger_factory=structlog.PrintLoggerFactory(),
)
structlog.contextvars.bind_contextvars(component="web")
log = structlog.get_logger("web.main")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
BUILD_DIR = Path(__file__).resolve().parent.parent / "build"
# D-94: instance/web/ permite que cada instancia sobrescreva assets PWA
# (icones, futuramente custom CSS) sem editar tracked code. Bind-mount em
# /workspace/web/ pelo compose. Override prevalece sobre framework default
# quando arquivo equivalente existe.
INSTANCE_WEB_DIR = Path(os.environ.get("INSTANCE_WEB_DIR", "/workspace/web"))


# ---------- Lifespan ----------

async def _bootstrap_admin_user():
    """Garante que o admin humano existe em messaging.users.

    Idempotente: ON CONFLICT atualiza is_admin/full_name. Quando
    ADMIN_PASSWORD vem setado no env, tambem atualiza password_hash
    (permite rotacao da senha via .env + restart, sem precisar mexer
    no banco). Quando vazio, preserva o hash existente — assim instancias
    que ja tinham senha valida nao sao zeradas se um restart venha sem
    a env.
    """
    email = os.environ.get("ADMIN_EMAIL", "admin@example.com")
    pwd = os.environ.get("ADMIN_PASSWORD", "")
    pwd_hash = bcrypt.hashpw(pwd.encode(), bcrypt.gensalt()).decode() if pwd else None
    await db.execute(
        """
        INSERT INTO messaging.users (email, username, full_name, kind, password_hash, is_admin)
        VALUES ($1, $2, $3, 'human', $4, true)
        ON CONFLICT (email) DO UPDATE SET
            is_admin = true,
            full_name = COALESCE(EXCLUDED.full_name, messaging.users.full_name),
            password_hash = COALESCE(EXCLUDED.password_hash, messaging.users.password_hash)
        """,
        email, email.split("@")[0], "Admin", pwd_hash,
    )
    log.info("web.admin_bootstrapped", email=email, password_set=bool(pwd_hash))


async def _bootstrap_system_bot():
    """User dedicado pra service tokens (reactor/scheduler) postarem mensagens.
    agent_name=NULL — nao aparece no /api/agents (sidebar do PWA).
    """
    await db.execute(
        """
        INSERT INTO messaging.users (email, username, full_name, kind, agent_name, is_admin)
        VALUES ('system-bot@internal.agent-framework', 'system-bot', 'System', 'bot', NULL, true)
        ON CONFLICT (email) DO UPDATE SET
            is_admin = true,
            agent_name = NULL
        """,
    )
    log.info("web.system_bot_bootstrapped")


async def _bootstrap_terminal_stream():
    """Garante que o stream agregador de terminais (task done/halt/human_review)
    existe, se configurado via env. Sem isso, reactor tenta postar em stream
    inexistente. Idempotente. No-op quando TERMINAL_NOTIFY_STREAM vazio.
    """
    terminal = os.environ.get("TERMINAL_NOTIFY_STREAM", "").strip()
    if not terminal:
        log.info("web.terminal_stream_skipped", reason="TERMINAL_NOTIFY_STREAM vazio")
        return
    await db.execute(
        """
        INSERT INTO messaging.streams (name, description)
        VALUES ($1, 'Notificacoes terminais de tasks (done/halt/human_review)')
        ON CONFLICT (name) DO NOTHING
        """,
        terminal,
    )
    log.info("web.terminal_stream_bootstrapped", stream=terminal)


async def _push_notifier_loop():
    """LISTEN em msg_all e dispara push VAPID APENAS quando ha pending_ask
    nao resolvido na conversa (ou seja, um agente esta bloqueado aguardando
    resposta do humano). Reply normal do bot nao gera push — aparece como
    unread discreto na sidebar ate o humano abrir.

    Racional: push e interrupcao — reservado pra demanda real de atencao
    (ask_human), nao pra cada bot reply/emoji de sinal.
    """
    dispatcher = app.state.push_dispatcher
    if dispatcher is None:
        log.info("web.push_notifier_disabled", reason="no_vapid")
        return

    dsn = os.environ["DATABASE_URL"]
    while True:
        try:
            conn = await asyncpg.connect(dsn)
            queue: asyncio.Queue[str] = asyncio.Queue()

            def _cb(_conn, _pid, _channel, payload):
                queue.put_nowait(payload)

            await conn.add_listener("msg_all", _cb)
            log.info("web.push_notifier_started")
            while True:
                try:
                    payload_raw = await asyncio.wait_for(queue.get(), timeout=60)
                except asyncio.TimeoutError:
                    continue
                try:
                    data = json.loads(payload_raw)
                    # Migration 026: echoes (D-100 forward) sao puramente
                    # visuais — pular push pra nao notificar humano de uma
                    # copia (a msg original na conv-filha ja teve sua propria
                    # avaliacao de pending_ask).
                    if data.get("kind") == "echo":
                        continue
                    meta = await db.fetch_one(
                        """SELECT u.username, u.kind AS sender_kind, s.name AS stream, c.topic_name AS topic,
                                  m.content
                             FROM messaging.users u
                             JOIN messaging.messages m ON m.sender_id = u.id
                             JOIN messaging.conversations c ON c.id = m.conversation_id
                             JOIN messaging.streams s ON s.id = c.stream_id
                            WHERE m.id = $1""",
                        data["id"],
                    )
                    if meta is None or meta["sender_kind"] != "bot":
                        continue
                    # D-111: filtra kind='ask_human' — ask_agent e
                    # silencioso pra humano (target agente responde).
                    has_pending = await db.fetch_one(
                        "SELECT 1 FROM messaging.pending_asks "
                        " WHERE conversation_id = $1 AND resolved_at IS NULL "
                        "   AND kind = 'ask_human'",
                        data["conversation_id"],
                    )
                    if not has_pending:
                        continue
                    content = (meta["content"] or "").strip()
                    first_line = content.splitlines()[0] if content else ""
                    title = f"{meta['username']} needs you"
                    body = f"#{meta['stream']} / {meta['topic']}\n{first_line[:160]}"
                    tag = f"{meta['stream']}/{meta['topic']}"
                    await dispatcher.broadcast(title=title, body=body, url="/", tag=tag)
                except Exception:
                    log.exception("web.push_notify_failed")
        except Exception:
            log.exception("web.push_notifier_crashed")
            await asyncio.sleep(5)
        finally:
            try:
                await conn.close()
            except Exception:
                pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_pool()
    auth_mod.init_service_tokens()
    await _bootstrap_admin_user()
    await _bootstrap_system_bot()
    await _bootstrap_terminal_stream()

    app.state.push_dispatcher = None
    vap_pub = os.environ.get("VAPID_PUBLIC_KEY")
    vap_priv = os.environ.get("VAPID_PRIVATE_KEY")
    if vap_pub and vap_priv:
        app.state.push_dispatcher = PushDispatcher(
            vapid_private_key=vap_priv,
            vapid_claims_sub=os.environ.get("VAPID_CONTACT_EMAIL", "mailto:admin@example.com"),
        )

    app.state.push_notifier_task = asyncio.create_task(_push_notifier_loop())

    log.info(
        "web.started",
        admin=os.environ.get("ADMIN_EMAIL"),
        vapid_enabled=bool(vap_pub),
        transcriber=os.environ.get("TRANSCRIBER_URL", "(disabled)"),
    )
    yield

    app.state.push_notifier_task.cancel()
    await db.close_pool()
    log.info("web.shutdown")


app = FastAPI(title="agent-framework web", version="0.2.0", lifespan=lifespan)
app.include_router(broker_router)
app.include_router(files_router)
app.include_router(scheduler_routes_router)


# ---------- Compat aliases pro PWA legacy ----------
# PWA espera formato {items: [...]} e ids como "stream/topic" (string).
# Broker retorna lista direta com id numerico. Aqui adaptamos.

def _conv_id_string(stream: str, topic: str) -> str:
    return f"{stream}/{topic}"


@app.get("/api/pending-asks")
async def pending_asks_alias(principal: Principal = Depends(get_principal)):
    from .broker import list_asks
    items = await list_asks(principal)
    # PWA espera id=stream/topic
    for it in items:
        it["id"] = _conv_id_string(it["stream"], it["topic"])
    return {"items": items}


@app.post("/api/post-message")
async def post_message_alias(payload: dict, principal: Principal = Depends(get_principal)):
    from .broker import MessageIn, post_message
    msg = MessageIn(
        stream=payload.get("stream"),
        topic=payload.get("topic") or "",
        content=payload.get("content", ""),
        client_id=payload.get("client_id"),
    )
    # Se topic veio vazio/None, cria um default com timestamp humanizado
    # (sem prefixo — entrada pode vir de voz ou texto, prefixo nao agrega).
    if not msg.topic:
        from datetime import datetime
        msg.topic = datetime.utcnow().strftime('%Y-%m-%d %H:%M')
    result = await post_message(msg, principal)
    return {
        "id": result.id, "stream": result.stream, "topic": result.topic,
        "conversation_id": result.conversation_id, "content": result.content,
        "sent_at": result.sent_at,
    }


@app.get("/api/conversations")
async def conversations_alias(
    filter: str = "active",
    principal: Principal = Depends(get_principal),
):
    """Lista unificada (D-57): retorna Active (archived_at IS NULL) ou Closed
    (archived_at IS NOT NULL). Task metadata vem junto quando a conversation
    e origem de uma task.

    Campos: `archived_at`, `participating`, `task: {...}` quando aplicavel."""
    from .broker import list_unified_conversations
    items = await list_unified_conversations(principal, filter_=filter)
    from datetime import datetime
    def _epoch(dt_iso: str) -> int:
        return int(datetime.fromisoformat(dt_iso.replace("Z", "+00:00")).timestamp())
    out = []
    for c in items:
        last_is_bot = (c["last_sender"] or "").endswith("-bot")
        ts = _epoch(c["last_message_at"])
        entry = {
            "id": _conv_id_string(c["stream"], c["topic"]),
            "db_id": c["id"],
            "agent": c["stream"],
            "stream": c["stream"],
            "topic": c["topic"],
            "custom_title": c.get("custom_title"),
            "last_activity": ts,
            "last_message_at": c["last_message_at"],
            "archived_at": c.get("archived_at"),
            "msg_count": c["msg_count"],
            "last_msg": {
                "sender": c["last_sender"],
                "is_bot": last_is_bot,
                "content_preview": (c["last_content"] or "")[:200],
                "content": c["last_content"] or "",
                "timestamp": ts,
            },
            "awaiting_human": c["awaiting_human"],
            "participating": c["participating"],
            # `closed` legado (web.closed_conversations, pre-D-57) nao se aplica
            # mais — filtro e archived_at. Mantido como False pra shape legacy.
            "closed": False,
            "parent_conv_id": c.get("parent_conv_id"),
            "children_stats": c.get("children_stats") or {
                "active": 0, "stuck": 0, "resolved": 0,
            },
            "is_running": bool(c.get("is_running")),
            "is_stuck": bool(c.get("is_stuck")),
            "is_errored": bool(c.get("is_errored")),
            "task_not_current_agent": bool(c.get("task_not_current_agent")),
        }
        if c.get("task"):
            entry["task"] = c["task"]
        out.append(entry)
    return {"items": out}


async def _resolve_conv_id(conv_id: str) -> int:
    """Aceita 'stream/topic' ou numero (string). Retorna id numerico."""
    if conv_id.isdigit():
        return int(conv_id)
    if "/" not in conv_id:
        raise HTTPException(status_code=400, detail="conv_id invalido (use stream/topic)")
    stream, topic = conv_id.split("/", 1)
    row = await db.fetch_one(
        """SELECT c.id FROM messaging.conversations c
             JOIN messaging.streams s ON s.id = c.stream_id
            WHERE s.name = $1 AND c.topic_name = $2""",
        stream, topic,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="conversa nao existe")
    return row["id"]


@app.get("/api/conversations/{conv_id:path}/messages")
async def conversation_messages_alias(conv_id: str, principal: Principal = Depends(get_principal)):
    numeric_id = await _resolve_conv_id(conv_id)
    rows = await db.fetch_all(
        """SELECT m.id, u.username AS sender, u.kind AS sender_kind,
                  m.content, m.sent_at,
                  s.name AS stream, c.topic_name AS topic, c.custom_title
             FROM messaging.messages m
             JOIN messaging.users u ON u.id = m.sender_id
             JOIN messaging.conversations c ON c.id = m.conversation_id
             JOIN messaging.streams s ON s.id = c.stream_id
            WHERE m.conversation_id = $1
            ORDER BY m.id ASC LIMIT 500""",
        numeric_id,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="conversa sem mensagens")
    first = rows[0]
    # D-111: kind='ask_human' — badge needs-you so dispara em ask humano,
    # nao em ask_agent (agente target responde, silencioso pra humano).
    pending = await db.fetch_one(
        "SELECT 1 FROM messaging.pending_asks "
        " WHERE conversation_id = $1 AND resolved_at IS NULL "
        "   AND kind = 'ask_human'",
        numeric_id,
    )
    # D-71: runner_state derivado de telemetry.live_events + pending_asks.
    # Mesmo shape do endpoint /runner-state standalone.
    runner_state = await _compute_runner_state(numeric_id)
    # D-96: parent_conv_id define read-only sozinho. Toda filha eh read-only
    # pro humano (D-96 collapsou regra antiga). Frontend deriva direto do
    # parent_conv_id; campo `read_only_reason` removido.
    parent_conv_id_row = await db.fetch_one(
        "SELECT parent_conv_id FROM messaging.conversations WHERE id = $1",
        numeric_id,
    )
    parent_conv_id = parent_conv_id_row["parent_conv_id"] if parent_conv_id_row else None
    return {
        "id": conv_id,
        "agent": first["stream"],
        "stream": first["stream"],
        "topic": first["topic"],
        "custom_title": first["custom_title"],
        "pending_ask_id": numeric_id if pending else None,
        "runner_state": runner_state,
        "parent_conv_id": parent_conv_id,
        "messages": [
            {
                "id": r["id"],
                "sender": r["sender"],
                "is_bot": r["sender_kind"] == "bot",
                "content": r["content"],
                "timestamp": int(r["sent_at"].timestamp()),
            } for r in rows
        ],
    }


async def _collect_descendant_conv_ids(root_id: int) -> list[int]:
    """Descobre recursivamente todas as convs descendentes de `root_id`.

    Fonte primaria (D-87): CTE recursivo no FK `parent_conv_id`. Lookup
    O(depth) em vez de heuristica por topic pattern. Captura netos e
    bisnetos que o parser legacy perdia (ex: topic
    `__ask-from-<avo>.<pai>-<uid>` nao batia no pattern
    `__ask-from-<pai>-%`).

    Fallback legacy: pra convs anteriores a D-87 (parent_conv_id NULL),
    mantem as heuristicas de D-86:
      1. ask_agent: convs com topic `__ask-from-<cur.stream>-<uid>` cujo
         tool_use correspondente em telemetry.live_events foi emitido
         pela conv corrente.
      2. task: se cur eh origem de uma task, todas as convs `task-<slug>`
         sao filhas.

    Retorna ids em ordem de descoberta (topdown).
    """
    # Fonte primaria: CTE recursivo.
    rows = await db.fetch_all(
        """WITH RECURSIVE tree AS (
               SELECT id FROM messaging.conversations WHERE id = $1
             UNION ALL
               SELECT c.id FROM messaging.conversations c
                 JOIN tree t ON c.parent_conv_id = t.id
           )
           SELECT id FROM tree WHERE id != $1""",
        root_id,
    )
    descendants: list[int] = [r["id"] for r in rows]
    visited: set[int] = {root_id, *descendants}

    # Fallback legacy: aplica heuristicas apenas em convs sem parent_conv_id
    # (pre-D-87). Se todas as convs ativas foram criadas pos-D-87, o loop
    # abaixo nao adiciona nada e o custo e desprezivel (uma query).
    legacy_roots_rows = await db.fetch_all(
        """SELECT id FROM messaging.conversations
            WHERE parent_conv_id IS NULL
              AND id = ANY($1::int[])""",
        [root_id, *descendants],
    )
    legacy_queue: list[int] = [r["id"] for r in legacy_roots_rows]

    while legacy_queue:
        cur_id = legacy_queue.pop(0)
        cur = await db.fetch_one(
            """SELECT s.name AS stream, c.topic_name AS topic
                 FROM messaging.conversations c
                 JOIN messaging.streams s ON s.id = c.stream_id
                WHERE c.id = $1""",
            cur_id,
        )
        if not cur:
            continue

        # Fase 1 legacy: ask_agent filhas (topic __ask-from-<cur_stream>-*).
        pattern = f"__ask-from-{cur['stream']}-%"
        candidates = await db.fetch_all(
            """SELECT c.id, c.created_at, s.name AS stream
                 FROM messaging.conversations c
                 JOIN messaging.streams s ON s.id = c.stream_id
                WHERE c.topic_name LIKE $1
                  AND c.parent_conv_id IS NULL""",
            pattern,
        )
        for cand in candidates:
            if cand["id"] in visited:
                continue
            tool_hit = await db.fetch_one(
                """SELECT le.id
                     FROM telemetry.live_events le
                    WHERE le.conversation_id = $1
                      AND le.kind = 'tool_use'
                      AND le.data::text LIKE '%ask_agent%'
                      AND (le.data->>'input') LIKE '%' || $2::text || '%'
                      AND le.ts <= ($3::timestamptz + interval '10 seconds')
                    LIMIT 1""",
                cur_id, cand["stream"], cand["created_at"],
            )
            if tool_hit:
                visited.add(cand["id"])
                descendants.append(cand["id"])
                legacy_queue.append(cand["id"])

        # Fase 2 legacy: task siblings.
        task_row = await db.fetch_one(
            """SELECT slug FROM tasks.tasks
                WHERE origin_stream = $1 AND origin_topic = $2""",
            cur["stream"], cur["topic"],
        )
        if task_row:
            task_topic = f"task-{task_row['slug']}"
            task_convs = await db.fetch_all(
                """SELECT c.id FROM messaging.conversations c
                    WHERE c.topic_name = $1 AND c.id != $2
                      AND c.parent_conv_id IS NULL""",
                task_topic, cur_id,
            )
            for tc in task_convs:
                if tc["id"] in visited:
                    continue
                visited.add(tc["id"])
                descendants.append(tc["id"])
                legacy_queue.append(tc["id"])

    return descendants


@app.post("/api/conversations/close-all")
async def conversations_close_all_alias(principal: Principal = Depends(get_principal)):
    from .broker import close_all_conversations
    return await close_all_conversations(principal)


async def _cancel_runners_for_convs(conv_ids: list[int], silent: bool = False) -> None:
    """Dispara `pg_notify('agent_ctrl', cancel_topic)` pra cada conversation.
    Agente listening no stream correspondente manda SIGTERM no processo
    Claude CLI ativo do topic (fallback SIGKILL apos 3s, via dispatcher).

    Chamado em archive/delete cascata — sem isso, runners ativos em convs
    removidas ficam fantasmas segurando slots de pool, travando tarefas
    futuras (D-72 bugfix).

    `silent=True` instrui o dispatcher a NAO postar a mensagem de
    confirmacao ("Cancelado pelo usuario" / "Nada pra cancelar"). Usado
    pelos callers archive/delete: a conv ja foi arquivada/apagada pelo
    humano, a notificacao seria ruido na tab Closed. Cancel manual via
    POST /api/conversations/{id}/cancel mantem `silent=False` — o
    feedback ali e util pra confirmar que o sinal foi processado.
    """
    if not conv_ids:
        return
    rows = await db.fetch_all(
        """SELECT s.name AS stream, c.topic_name AS topic
             FROM messaging.conversations c
             JOIN messaging.streams s ON s.id = c.stream_id
            WHERE c.id = ANY($1::int[])""",
        conv_ids,
    )
    for r in rows:
        payload = json.dumps({
            "type": "cancel_topic",
            "stream": r["stream"],
            "topic": r["topic"],
            "user_id": None,
            "silent": silent,
        })
        await db.execute("SELECT pg_notify('agent_ctrl', $1)", payload)


@app.patch("/api/conversations/{conv_id:path}")
async def conversation_patch(
    conv_id: str,
    payload: dict,
    _: Principal = Depends(get_principal),
):
    """Atualiza metadata editavel da conversa. Hoje aceita so `custom_title`
    (titulo amigavel definido pelo humano via PWA — display rule no frontend
    e `custom_title || task.title || topic`). Passar string vazia ou null
    apaga o override (volta pro fallback). Migration 027."""
    if "custom_title" not in payload:
        raise HTTPException(status_code=400, detail="custom_title obrigatorio")
    raw = payload.get("custom_title")
    if raw is None:
        new_title: str | None = None
    elif isinstance(raw, str):
        trimmed = raw.strip()
        new_title = trimmed if trimmed else None
    else:
        raise HTTPException(status_code=400, detail="custom_title deve ser string ou null")
    if new_title is not None and len(new_title) > 200:
        raise HTTPException(status_code=400, detail="custom_title acima de 200 caracteres")
    numeric_id = await _resolve_conv_id(conv_id)
    row = await db.fetch_one(
        """UPDATE messaging.conversations
              SET custom_title = $2
            WHERE id = $1
        RETURNING id, custom_title""",
        numeric_id, new_title,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="conversa nao existe")
    return {"ok": True, "id": row["id"], "custom_title": row["custom_title"]}


@app.post("/api/conversations/{conv_id:path}/archive")
async def conversation_archive(conv_id: str, principal: Principal = Depends(get_principal)):
    """D-57: soft-delete manual de thread pelo humano. Move pra tab Closed no
    PWA. Diferente de task archive (que exige status terminal), conversation
    archive e livre — e decisao do humano, nao da mecanica da task.

    D-72: cascade — descendentes (sub-conversas via ask_agent + convs
    `task-<slug>` da mesma task) sao arquivadas junto. Runners ativos das
    convs envolvidas recebem cancel via agent_ctrl pra nao ficarem
    fantasmas segurando o pool.

    Bot caller (MCP `archive_conversation`): adicionalmente bloqueia se
    o proprio bot tem `pending_ask` aberto na conv — arquivar silenciaria
    o humano antes da resposta. Humano caller (PWA) nao tem essa regra
    (decisao livre)."""
    numeric_id = await _resolve_conv_id(conv_id)
    row = await db.fetch_one(
        "SELECT archived_at FROM messaging.conversations WHERE id = $1",
        numeric_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="conversa nao existe")
    if row["archived_at"] is not None:
        raise HTTPException(status_code=409, detail="conversa ja esta arquivada")
    if principal.kind == "bot" and principal.user_id is not None:
        # D-111: bloqueia archive apenas se ha ask_human pendente proprio.
        # ask_agent pendente (cross-agent) nao deve bloquear archive — bot
        # arquivar significa "abandono o ask".
        own_pending = await db.fetch_one(
            """SELECT 1 FROM messaging.pending_asks
                WHERE conversation_id = $1
                  AND asker_id = $2
                  AND resolved_at IS NULL
                  AND kind = 'ask_human'""",
            numeric_id, principal.user_id,
        )
        if own_pending:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Voce tem um ask_human aberto nesta conversa — arquivar "
                    "silenciaria o humano antes da resposta. Aguarde a "
                    "resposta ou resolva o ask antes de chamar archive."
                ),
            )
    descendants = await _collect_descendant_conv_ids(numeric_id)
    all_ids = [numeric_id, *descendants]
    await _cancel_runners_for_convs(all_ids, silent=True)
    result = await db.execute(
        """UPDATE messaging.conversations
              SET archived_at = now()
            WHERE id = ANY($1::int[])
              AND archived_at IS NULL""",
        all_ids,
    )
    log.info(
        "conversation_archived",
        conv_id=numeric_id,
        cascaded=len(descendants),
        sql=result,
    )
    return {
        "ok": True,
        "id": numeric_id,
        "archived": True,
        "cascaded_ids": descendants,
    }


@app.post("/api/conversations/{conv_id:path}/unarchive")
async def conversation_unarchive(conv_id: str, _: Principal = Depends(get_principal)):
    """Reabre conversa: volta pra Active. Se havia task atrelada em status
    terminal, a task NAO e reaberta automaticamente — isso e acao explicita
    (MCP tool reopen_task, D-57 fase 2.5) ou nova task.

    D-72: cascade — descendentes arquivadas tambem voltam pra Active
    (idempotente)."""
    numeric_id = await _resolve_conv_id(conv_id)
    row = await db.fetch_one(
        "SELECT archived_at FROM messaging.conversations WHERE id = $1",
        numeric_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="conversa nao existe")
    if row["archived_at"] is None:
        raise HTTPException(status_code=409, detail="conversa nao esta arquivada")
    descendants = await _collect_descendant_conv_ids(numeric_id)
    all_ids = [numeric_id, *descendants]
    result = await db.execute(
        """UPDATE messaging.conversations
              SET archived_at = NULL
            WHERE id = ANY($1::int[])
              AND archived_at IS NOT NULL""",
        all_ids,
    )
    log.info(
        "conversation_unarchived",
        conv_id=numeric_id,
        cascaded=len(descendants),
        sql=result,
    )
    return {
        "ok": True,
        "id": numeric_id,
        "archived": False,
        "cascaded_ids": descendants,
    }


@app.delete("/api/conversations/{conv_id:path}")
async def conversation_delete_alias(conv_id: str, principal: Principal = Depends(get_principal)):
    """DELETE permanente. Cascade apaga msgs + pending_asks + closed refs
    de cada conversation. D-72: agora tambem deleta descendentes em cascata
    (sub-conversas ask_agent + convs da mesma task). Runners ativos dos
    topics sao cancelados via agent_ctrl antes do DELETE, pra nao ficarem
    fantasmas segurando slot de pool do agente."""
    if principal.user_id is None:
        raise HTTPException(status_code=401, detail="nao autenticado")
    numeric_id = await _resolve_conv_id(conv_id)
    descendants = await _collect_descendant_conv_ids(numeric_id)
    all_ids = [numeric_id, *descendants]
    # Cancela ANTES do DELETE — dispatcher resolve stream+topic pelo payload
    # do pg_notify, nao precisa que a conv ainda exista quando o SIGTERM
    # chega, mas pesquisar stream/topic no DB exige conv viva. Ordem importa.
    # silent=True: conv vai sumir (DELETE), confirmacao seria ruido.
    await _cancel_runners_for_convs(all_ids, silent=True)
    # Grava tombstone pra bloquear re-criacao por 5min (D-72 fix): qualquer
    # msg em voo (agente ainda processando turno, reactor com handoff
    # pendente, etc) chegando apos o DELETE poderia auto-criar a conv de
    # novo no broker. Tombstone antes do DELETE garante que o INSERT casa
    # stream_id/topic_name enquanto a conv ainda existe.
    await db.execute(
        """INSERT INTO messaging.deleted_topics (stream_id, topic_name, deleted_at)
            SELECT c.stream_id, c.topic_name, now()
              FROM messaging.conversations c
             WHERE c.id = ANY($1::int[])
            ON CONFLICT (stream_id, topic_name)
              DO UPDATE SET deleted_at = now()""",
        all_ids,
    )
    # Snapshot agregado do trace em telemetry.events.metadata ANTES do DELETE.
    # Razao: telemetry.live_events tem FK CASCADE pra messaging.conversations
    # e e apagado junto com a conv. O summary em telemetry.events sobrevive
    # (FK SET NULL, migration 020) mas o trace bruto some. Aqui preservamos
    # o agregado ("quantos thinkings, quantos tool_use, quais tools usadas")
    # em metadata — suficiente pra task detail continuar mostrando effort
    # pos-delete. O UPDATE casa via conversation_id OU via topic_slug (fallback
    # pra rows cujo conversation_id ainda nao foi populado — ate o runner ser
    # rebuildado, a maioria dos rows nao vai ter FK resolvida).
    await db.execute(
        """WITH trace_agg AS (
            SELECT le.conversation_id,
                   s.name      AS stream_name,
                   c.topic_name,
                   COUNT(*) FILTER (WHERE kind = 'thinking')  AS thinking_count,
                   COUNT(*) FILTER (WHERE kind = 'tool_use')  AS tool_use_count,
                   jsonb_agg(DISTINCT le.data->>'name')
                     FILTER (WHERE kind = 'tool_use' AND le.data ? 'name') AS tools_used
              FROM telemetry.live_events le
              JOIN messaging.conversations c ON c.id = le.conversation_id
              JOIN messaging.streams       s ON s.id = c.stream_id
             WHERE le.conversation_id = ANY($1::int[])
             GROUP BY le.conversation_id, s.name, c.topic_name
        )
        UPDATE telemetry.events e
           SET metadata = COALESCE(e.metadata, '{}'::jsonb) || jsonb_build_object(
                 'trace_snapshot', jsonb_build_object(
                     'thinking_count', ta.thinking_count,
                     'tool_use_count', ta.tool_use_count,
                     'tools_used',     COALESCE(ta.tools_used, '[]'::jsonb),
                     'snapshot_at',    extract(epoch from now())
                 )
               )
          FROM trace_agg ta
         WHERE e.conversation_id = ta.conversation_id
            OR e.topic_slug = ta.stream_name || '__' || ta.topic_name""",
        all_ids,
    )
    result = await db.execute(
        "DELETE FROM messaging.conversations WHERE id = ANY($1::int[])",
        all_ids,
    )
    log.info(
        "conversation_deleted",
        conv_id=numeric_id,
        cascaded=len(descendants),
        sql=result,
    )
    return {"ok": True, "result": result, "cascaded_ids": descendants}


@app.post("/api/conversations/{conv_id:path}/close")
async def conversation_close_alias(conv_id: str, principal: Principal = Depends(get_principal)):
    from .broker import close_conversation
    numeric_id = await _resolve_conv_id(conv_id)
    return await close_conversation(numeric_id, principal)


@app.post("/api/conversations/{conv_id:path}/cancel")
async def conversation_cancel(conv_id: str, principal: Principal = Depends(get_principal)):
    """Cancela turn pendente no agente (se ainda nao comecou a chamar Claude).

    D-29: dispara NOTIFY no canal Postgres `agent_ctrl` com payload
    `{type, stream, topic, user_id}`. Agente filtra por stream que escuta,
    passa pro dispatcher, que decide (nada/tarde demais/cancela + confirma).
    D-71: quando Claude CLI esta rodando, dispatcher agora faz SIGTERM no
    proc ao inves de responder "tarde demais".
    """
    if principal.user_id is None:
        raise HTTPException(status_code=401, detail="nao autenticado")
    numeric_id = await _resolve_conv_id(conv_id)
    row = await db.fetch_one(
        """SELECT s.name AS stream, c.topic_name AS topic
             FROM messaging.conversations c
             JOIN messaging.streams s ON s.id = c.stream_id
            WHERE c.id = $1""",
        numeric_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="conversa nao existe")
    payload = json.dumps({
        "type": "cancel_topic",
        "stream": row["stream"],
        "topic": row["topic"],
        "user_id": principal.user_id,
    })
    # `pg_notify` eh fire-and-forget — agente pode nao estar listening
    # (reply "nada pra cancelar" vem do agente se receber).
    await db.execute("SELECT pg_notify('agent_ctrl', $1)", payload)
    return {"ok": True}


# ---------- D-71: runner state + retry ----------

# Threshold pra marcar um turn como "stuck": ultimo live_event (run_start,
# thinking, tool_use, tool_result) ha mais de X segundos sem run_end. Default
# 10min — Claude CLI respeita MCP_TOOL_TIMEOUT de 24h pra ask_human/ask_agent,
# mas esses nao quebram run_start sem eventos intermediarios (thinking/tool_use
# sao emitidos durante a pausa). 10min cobre ciclo normal de run ativa; alem
# disso vira candidato a cancel/retry.
RUNNER_STUCK_SEC = int(os.environ.get("RUNNER_STUCK_SEC", "600"))


async def _compute_runner_state(conv_id: int) -> dict:
    """Deriva o estado corrente do runner pra uma conversation.

    Fonte: `messaging.runs` (single source of truth, migration 030) +
    `messaging.pending_asks` (ask_human ativo). O dual-write no handler
    de telemetry.live_events mantem `runs` em sync; o reaper do scheduler
    fecha runs orfas como 'stale'.

    Estados:
      - `idle`: nenhuma run ou ultima run terminou (done/stale).
      - `running`: ultima run com status='running' e heartbeat fresco.
      - `errored`: ultima run com status='error'.
      - `blocked_on_ask_human`: ha pending_ask kind='ask_human' nao resolvido.
      - `stuck`: status='running' mas heartbeat antigo (> RUNNER_STUCK_SEC).
    """
    run = await db.fetch_one(
        """SELECT status, started_at, last_heartbeat_at, finished_at, exit_reason
             FROM messaging.runs
            WHERE conversation_id = $1
            ORDER BY started_at DESC
            LIMIT 1""",
        conv_id,
    )
    # D-111: blocked_on_ask_human = literalmente bloqueado em humano.
    # ask_agent pendente nao gera estado "blocked" pra UI (eh transicao
    # interna entre agentes, nao espera de humano).
    pending = await db.fetch_one(
        "SELECT asked_at FROM messaging.pending_asks "
        "WHERE conversation_id = $1 AND resolved_at IS NULL "
        "  AND kind = 'ask_human'",
        conv_id,
    )

    now = time.time()
    state = "idle"
    since = None
    last_error = None
    stuck = False

    # D-84: awaiting_human tem precedencia absoluta sobre running/stuck.
    # Quando ha pending_ask, o runner pode estar vivo bloqueado no callback,
    # mas operacionalmente nada avanca ate o humano responder.
    if pending is not None:
        state = "awaiting_human"
        since = pending["asked_at"]
    elif run is not None:
        if run["status"] == "running":
            since = run["started_at"]
            elapsed = now - run["last_heartbeat_at"].timestamp()
            if elapsed > RUNNER_STUCK_SEC:
                state = "stuck"
                stuck = True
            else:
                state = "running"
        elif run["status"] == "error":
            state = "errored"
            since = run["finished_at"] or run["started_at"]
            last_error = run["exit_reason"]
        # done / stale → idle (default)

    can_retry = state in ("errored", "stuck")
    # Cancel so faz sentido quando ha computacao em andamento (Claude CLI
    # ativo, nao bloqueado em ask_human). `blocked_on_ask_human` tem o CLI
    # vivo mas bloqueado no callback do broker — SIGTERM ali deixaria
    # pending_ask dangling e confundiria o usuario ("cancelar" soa como
    # "interromper trabalho", mas nao ha trabalho acontecendo, so espera).
    # UX correto nesse estado: responder a pergunta ou archive.
    can_cancel = state in ("running", "stuck")
    return {
        "state": state,
        "since": since.isoformat() if since is not None else None,
        "last_error": last_error,
        "can_retry": can_retry,
        "can_cancel": can_cancel,
        "stuck": stuck,
    }


@app.get("/api/conversations/{conv_id:path}/runner-state")
async def conversation_runner_state(
    conv_id: str, _: Principal = Depends(get_principal),
):
    """Retorna o estado corrente do runner pra uma conversation.

    Usado pelo PWA pra renderizar badge de estado e habilitar botoes
    Retry/Cancel. Invalidado via SSE quando chega run_start/run_end/thinking.
    """
    numeric_id = await _resolve_conv_id(conv_id)
    return await _compute_runner_state(numeric_id)


@app.post("/api/conversations/{conv_id:path}/retry")
async def conversation_retry(
    conv_id: str, principal: Principal = Depends(get_principal),
):
    """Re-processa o ultimo turno do topic como se fosse uma mensagem nova.

    D-71: busca a ultima mensagem cujo sender nao seja o bot da propria
    stream (evita loop — nao re-dispara uma resposta do proprio agente como
    trigger) e reemite `pg_notify('msg_stream_<sid>', <payload>)`. O callback
    `_on_notify` no agente nao dedup por message_id, entao roda o handler de
    novo — mesmo prompt, session_id atual no DB (pode estar None apos D-70
    recovery, que aciona fresh start).

    Guard: so permite se `can_retry` (errored/stuck). Senao 409 — evita
    double-dispatch acidental em run ativa.
    """
    if principal.user_id is None:
        raise HTTPException(status_code=401, detail="nao autenticado")
    numeric_id = await _resolve_conv_id(conv_id)
    state = await _compute_runner_state(numeric_id)
    if not state["can_retry"]:
        raise HTTPException(
            status_code=409,
            detail=f"retry indisponivel no estado atual: {state['state']}",
        )
    # Busca conversation + stream_id + nome do bot da stream (= nome do
    # agente, convencao). Ignoramos mensagens desse bot pra achar o ultimo
    # trigger externo.
    conv = await db.fetch_one(
        """SELECT s.id AS stream_id, s.name AS stream_name, c.topic_name AS topic
             FROM messaging.conversations c
             JOIN messaging.streams s ON s.id = c.stream_id
            WHERE c.id = $1""",
        numeric_id,
    )
    if conv is None:
        raise HTTPException(status_code=404, detail="conversa nao existe")
    # Ultima msg cujo sender nao seja o proprio agente (bot com agent_name =
    # stream_name). Fallback: qualquer ultima msg.
    trigger = await db.fetch_one(
        """SELECT m.id, m.sender_id, m.conversation_id
             FROM messaging.messages m
             JOIN messaging.users u ON u.id = m.sender_id
            WHERE m.conversation_id = $1
              AND NOT (u.kind = 'bot' AND u.agent_name = $2)
            ORDER BY m.id DESC LIMIT 1""",
        numeric_id, conv["stream_name"],
    )
    if trigger is None:
        # Fallback: ultima msg qualquer (caso de conversa so com msgs do
        # proprio agente — raro mas possivel em loops de automacao).
        trigger = await db.fetch_one(
            "SELECT id, sender_id, conversation_id FROM messaging.messages "
            "WHERE conversation_id = $1 ORDER BY id DESC LIMIT 1",
            numeric_id,
        )
        if trigger is None:
            raise HTTPException(
                status_code=400,
                detail="conversa sem mensagens pra re-processar",
            )
    # Reemite o mesmo shape de payload que o trigger `messaging.notify_message`
    # produz (ver migrations/007_notify_payload_slim.sql).
    payload = json.dumps({
        "id": trigger["id"],
        "conversation_id": trigger["conversation_id"],
        "sender_id": trigger["sender_id"],
    })
    await db.execute(
        "SELECT pg_notify('msg_stream_' || $1, $2)",
        str(conv["stream_id"]), payload,
    )
    log.info(
        "web.retry_dispatched",
        conv_id=numeric_id, message_id=trigger["id"],
        stream=conv["stream_name"], topic=conv["topic"],
    )
    return {"ok": True, "dispatched": True, "message_id": trigger["id"]}


# ---------- Auth ----------

# D-95 followup: brute-force throttle.
#
# Estado in-memory por email — single-process (uvicorn 1 worker default),
# perdido em restart (aceito: brute-force quem reinicia o web pra resetar
# o contador ja teve que entrar no host, jogo perdido). Contador eh lista
# de timestamps de falhas, com janela deslizante de _LOGIN_WINDOW_SEC.
# 3+ falhas dentro da janela aplicam delay 2^(count-2)s capado em 30s,
# antes de retornar 401. Sucesso zera o contador daquele email.
#
# Hash dummy abaixo eh usado pra constant-time response quando o email
# nao existe — sem isso, atacante mede latencia (no-bcrypt vs bcrypt) e
# enumera quais emails tem conta.
_LOGIN_WINDOW_SEC = 900  # 15 minutos
_LOGIN_THROTTLE_AFTER = 3
_LOGIN_DELAY_CAP_SEC = 30
_login_failures: dict[str, list[float]] = {}
# bcrypt hash de string aleatoria — nunca bate com password real do user
_DUMMY_PWD_HASH = bcrypt.hashpw(os.urandom(32), bcrypt.gensalt()).decode()


def _login_failure_count(email: str) -> int:
    """Quantas falhas consecutivas dentro da janela. Side-effect: prune
    entries antigas do email."""
    now = time.time()
    cutoff = now - _LOGIN_WINDOW_SEC
    fails = [t for t in _login_failures.get(email, []) if t > cutoff]
    if fails:
        _login_failures[email] = fails
    else:
        _login_failures.pop(email, None)
    return len(fails)


def _record_login_failure(email: str) -> int:
    """Registra falha e retorna count atualizado."""
    now = time.time()
    fails = _login_failures.get(email, [])
    cutoff = now - _LOGIN_WINDOW_SEC
    fails = [t for t in fails if t > cutoff]
    fails.append(now)
    _login_failures[email] = fails
    return len(fails)


def _clear_login_failures(email: str) -> None:
    _login_failures.pop(email, None)


def _real_client_ip(request: Request) -> str:
    """Extrai IP real do cliente.

    Atras de cloudflared, request.client.host eh o IP do container
    cloudflared (rede docker bridge); o IP real vem no header
    `CF-Connecting-IP`. Sem cloudflared (acesso direto a 9090),
    request.client.host JA eh o IP real.

    Trust de header eh CONDICIONAL: so confiamos em CF-Connecting-IP
    / X-Forwarded-For quando o source eh private/loopback (cloudflared
    no docker network ou localhost). Se o request veio de um IP publico
    direto, ignoramos o header — qualquer um podia spoofar.
    """
    direct = request.client.host if request.client else None
    trust_proxy_headers = False
    if direct:
        try:
            from ipaddress import ip_address
            ip = ip_address(direct)
            trust_proxy_headers = ip.is_private or ip.is_loopback
        except ValueError:
            trust_proxy_headers = False
    if trust_proxy_headers:
        cf = request.headers.get("cf-connecting-ip")
        if cf:
            return cf
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return direct or "?"


@app.post("/api/auth/login")
async def auth_login(payload: dict, request: Request, response: Response):
    """Login humano: bcrypt check + cria session + set cookie HttpOnly.

    Anti-brute (D-95): contador in-memory por email aplica delay
    progressivo apos 3 falhas dentro de janela de 15min. Constant-time
    bcrypt (roda mesmo quando user nao existe) elimina enumeracao via
    timing.
    """
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    if not email or not password:
        raise HTTPException(status_code=400, detail="email e password obrigatorios")

    # Throttle ANTES da query/bcrypt — atacante nao acelera com 50 reqs/seg.
    fail_count = _login_failure_count(email)
    if fail_count >= _LOGIN_THROTTLE_AFTER:
        delay = min(2 ** (fail_count - _LOGIN_THROTTLE_AFTER + 1), _LOGIN_DELAY_CAP_SEC)
        log.warning(
            "web.auth.throttled",
            email=email, ip=_real_client_ip(request),
            fail_count=fail_count, delay_sec=delay,
        )
        await asyncio.sleep(delay)

    row = await db.fetch_one(
        """SELECT id, username, password_hash FROM messaging.users
            WHERE LOWER(email) = $1 AND kind = 'human'""",
        email,
    )
    # Constant-time: roda bcrypt mesmo se user nao existe ou hash NULL.
    # Resposta 401 sempre passa por ~100ms de bcrypt, indistinguivel do
    # caso "user existe, senha errada".
    target_hash = row["password_hash"] if (row and row["password_hash"]) else _DUMMY_PWD_HASH
    try:
        bcrypt_ok = bcrypt.checkpw(password.encode(), target_hash.encode())
    except Exception:
        bcrypt_ok = False
    ok = bool(row and row["password_hash"] and bcrypt_ok)

    if not ok:
        new_count = _record_login_failure(email)
        log.warning(
            "web.auth.login_failed",
            email=email, ip=_real_client_ip(request),
            fail_count=new_count, user_exists=bool(row),
        )
        raise HTTPException(status_code=401, detail="credenciais invalidas")

    _clear_login_failures(email)
    ua = request.headers.get("user-agent")
    token, expires_at = await create_session(row["id"], user_agent=ua)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_DAYS * 24 * 3600,
        httponly=True,
        secure=cookie_secure(),
        samesite="lax",
        path="/",
    )
    log.info(
        "web.auth.login", user_id=row["id"], username=row["username"],
        ip=_real_client_ip(request),
    )
    return {"ok": True, "username": row["username"], "expires_at": expires_at.isoformat()}


@app.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response):
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        await delete_session(cookie)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
async def auth_me(principal: Principal = Depends(get_principal)):
    return {
        "user_id": principal.user_id,
        "username": principal.username,
        "kind": principal.kind,
        "is_admin": principal.is_admin,
    }


# ---------- Tasks (unified view across conversations) ----------
# Schema vive em Postgres (tasks.tasks + tasks.phases + tasks.worktrees,
# migration 006 — D-53). Artifacts (.md) continuam em company/tasks/<slug>/
# porque agentes escrevem via Write tool e PWA renderiza. Arquivamento eh
# soft-delete (archived_at); delete purga a task + phases (CASCADE) +
# orchestrator.events + diretorio de artifacts.

_TASKS_ARTIFACT_DIR = Path("/workspace/company/tasks")

import re as _re
_SLUG_RE = _re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
_TERMINAL_STATUSES = {"done", "halt", "human_review", "blocked"}


def _validate_slug(slug: str) -> str:
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="invalid slug format")
    return slug


async def _fetch_task_row(slug: str) -> dict | None:
    _validate_slug(slug)
    row = await db.fetch_one(
        """SELECT id, slug, title, workflow, status, current_step, current_agent,
                  complexity, impact, difficulty, origin, origin_stream, origin_topic,
                  blocked_reason, metadata_extra, archived_at, created_at, updated_at
             FROM tasks.tasks WHERE slug = $1""",
        slug,
    )
    return dict(row) if row else None


@app.get("/api/tasks")
async def tasks_list(
    include_archived: int = 0,
    _: Principal = Depends(get_principal),
):
    """Lista tasks do Postgres (schema tasks.*, migration 006). Ordenado por
    updated_at desc. `include_archived=1` inclui tambem as soft-deleted
    (archived_at IS NOT NULL). Campos compativeis com o shape legacy do PWA
    pra zero mudanca no frontend."""
    where_clause = "" if include_archived else "WHERE t.archived_at IS NULL"
    rows = await db.fetch_all(
        f"""SELECT t.slug, t.title, t.status, t.current_step, t.current_agent,
                   t.workflow, t.updated_at, t.archived_at,
                   COALESCE((SELECT COUNT(*) FROM tasks.phases p
                              WHERE p.task_id = t.id AND p.completed_at IS NOT NULL), 0) AS phases_count
              FROM tasks.tasks t
              {where_clause}
             ORDER BY t.updated_at DESC""",
    )
    items = [
        {
            "slug": r["slug"],
            "title": r["title"],
            "status": r["status"],
            "current_step": r["current_step"],
            "current_agent": r["current_agent"],
            "workflow": r["workflow"],
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            "phases_count": int(r["phases_count"] or 0),
            "archived": r["archived_at"] is not None,
        }
        for r in rows
    ]
    return {"items": items}


@app.post("/api/tasks/{slug}/archive")
async def task_archive(slug: str, _: Principal = Depends(get_principal)):
    """Soft-delete: marca archived_at = now() se status eh terminal.
    Task viva (in_progress) nao pode ser arquivada — orquestrador ainda
    pode estar despachando."""
    row = await _fetch_task_row(slug)
    if row is None:
        raise HTTPException(status_code=404, detail=f"task '{slug}' não encontrada")
    if row["archived_at"] is not None:
        raise HTTPException(status_code=409, detail="task já está arquivada")
    if row["status"] not in _TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"só dá pra arquivar task em status terminal; status atual: {row['status']!r}",
        )
    await db.execute(
        "UPDATE tasks.tasks SET archived_at = now() WHERE slug = $1",
        slug,
    )
    log.info("task_archived", slug=slug, status=row["status"])
    return {"ok": True, "slug": slug, "archived": True}


@app.post("/api/tasks/{slug}/unarchive")
async def task_unarchive(slug: str, _: Principal = Depends(get_principal)):
    """Unset archived_at."""
    row = await _fetch_task_row(slug)
    if row is None:
        raise HTTPException(status_code=404, detail=f"task '{slug}' não encontrada")
    if row["archived_at"] is None:
        raise HTTPException(status_code=409, detail="task não está arquivada")
    await db.execute(
        "UPDATE tasks.tasks SET archived_at = NULL WHERE slug = $1",
        slug,
    )
    log.info("task_unarchived", slug=slug)
    return {"ok": True, "slug": slug, "archived": False}


@app.delete("/api/tasks/{slug}")
async def task_delete(slug: str, _: Principal = Depends(get_principal)):
    """Delete permanente. Só permitido se a task estiver ARQUIVADA.
    Apaga row (CASCADE em phases/worktrees), purga orchestrator.events da
    task, e remove o diretorio de artifacts do filesystem."""
    import shutil as _shutil
    row = await _fetch_task_row(slug)
    if row is None:
        raise HTTPException(status_code=404, detail=f"task '{slug}' não encontrada")
    if row["archived_at"] is None:
        raise HTTPException(
            status_code=409,
            detail="task ativa; arquive primeiro antes de deletar",
        )
    events_purged = await db.execute(
        "DELETE FROM orchestrator.events WHERE task_slug = $1",
        slug,
    )
    await db.execute("DELETE FROM tasks.tasks WHERE slug = $1", slug)
    # Remove artifacts dir (se existir). Safety: path resolvido tem que
    # comecar pelo prefix — defesa contra slug com .. (ja validado por
    # _SLUG_RE, mas backup em profundidade).
    artifacts = _TASKS_ARTIFACT_DIR / slug
    if artifacts.is_dir() and str(artifacts.resolve()).startswith(
        str(_TASKS_ARTIFACT_DIR.resolve()) + os.sep
    ):
        _shutil.rmtree(artifacts, ignore_errors=True)
    log.info("task_deleted", slug=slug, events_purged=str(events_purged))
    return {"ok": True, "slug": slug, "deleted": True}


# ---------- Workflows (D-57 — progress bar dinamica no PWA) ----------
# Expõe o yaml de workflows da instancia pra o frontend renderizar
# progress bar sem conhecer o vocabulario. Framework permanece agnostico:
# so resolve estrutura (ordem happy-path, terminais), nunca hardcoda
# nomes de step.

import yaml as _yaml

_WORKFLOWS_YAML_PATH = Path("/workspace/company/workflows.yaml")
_WORKFLOW_TERMINALS = {"done", "halt", "human_review"}
_WORKFLOW_NAME_RE = _re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_WORKFLOW_VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_WORKFLOWS_YAML_MAX_BYTES = 256 * 1024  # cap mais alto que company files — workflow yaml cresce com steps
_WORKFLOWS_YAML_HEADER = (
    "# workflows.yaml — taxonomia de fases desta instancia.\n"
    "#\n"
    "# Este arquivo eh gerenciado pelo PWA (Settings -> Workflows). Edicao\n"
    "# direta no disco funciona, mas comentarios e formatacao serao\n"
    "# reescritos no proximo save via UI. Prefira usar a interface.\n"
    "#\n"
    "# Framework so conhece os terminais (done | halt | human_review). Tudo\n"
    "# mais (nomes de step, agentes default, artifacts, transicoes) eh\n"
    "# convencao desta instancia.\n"
    "\n"
)


def _load_workflows_raw() -> dict:
    if not _WORKFLOWS_YAML_PATH.exists():
        return {}
    with _WORKFLOWS_YAML_PATH.open("r", encoding="utf-8") as f:
        raw = _yaml.safe_load(f) or {}
    return raw.get("workflows") or {}


def _load_workflows_full() -> dict:
    """Retorna o documento YAML inteiro (nao so a chave `workflows`).
    Usado pelos endpoints de write, que precisam preservar metadados
    fora de `workflows:` se a instancia tiver adicionado algum."""
    if not _WORKFLOWS_YAML_PATH.exists():
        return {"workflows": {}}
    with _WORKFLOWS_YAML_PATH.open("r", encoding="utf-8") as f:
        raw = _yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("workflows", {})
    if not isinstance(raw["workflows"], dict):
        raw["workflows"] = {}
    return raw


class _WorkflowYamlDumper(_yaml.SafeDumper):
    """SafeDumper custom que renderiza strings multilinha em block style (`|`)
    em vez de fluxo com `\\n` escapado. Usado pra `instructions` (markdown
    multilinha) ficar legivel no yaml em disco."""


def _str_representer(dumper, data: str):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_WorkflowYamlDumper.add_representer(str, _str_representer)


def _dump_workflows_yaml(full: dict) -> str:
    """Serializa com header fixo + dumper custom preservando ordem e
    rendering markdown multilinha como block scalar."""
    body = _yaml.dump(
        full,
        Dumper=_WorkflowYamlDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    return _WORKFLOWS_YAML_HEADER + body


def _write_workflows_yaml(full: dict) -> int:
    text = _dump_workflows_yaml(full)
    encoded = text.encode("utf-8")
    if len(encoded) > _WORKFLOWS_YAML_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"workflows.yaml excede {_WORKFLOWS_YAML_MAX_BYTES} bytes",
        )
    _WORKFLOWS_YAML_PATH.parent.mkdir(parents=True, exist_ok=True)
    _WORKFLOWS_YAML_PATH.write_text(text, encoding="utf-8")
    return len(encoded)


def _normalize_workflow_body(body: dict) -> dict:
    """Normaliza o dict de entrada pra serializacao yaml determinista.

    - Dropa campos desconhecidos no topo, mantendo apenas `initial_step`,
      `steps` e `description` (opcional).
    - Em cada step, mantem apenas `agent`, `artifact`, `next`, `instructions`.
    - `agent`/`artifact`/`instructions` ausentes ou string vazia => omitidos
      no yaml (equivalem a None => "exige next_agent explicito" / "sem artifact
      default" / "sem bloco de instrucoes da fase").
    - `next` sempre lista de strings unicas, ordem preservada.
    - `instructions` aceita string multilinha (markdown). Cap em 32 KB pra
      evitar payload abusivo no prompt.
    """
    out: dict = {}
    if isinstance(body.get("description"), str) and body["description"].strip():
        out["description"] = body["description"].strip()
    # orchestrator (D-110): agente dono da conv-supervisora; raiz fixa
    # da hierarquia de convs durante a vida da task. Opcional — sem ele,
    # framework cai pro initial_step.agent (compat com workflows pre-D-110).
    orchestrator = body.get("orchestrator")
    if isinstance(orchestrator, str) and orchestrator.strip():
        out["orchestrator"] = orchestrator.strip()
    out["initial_step"] = str(body.get("initial_step") or "")
    steps_in = body.get("steps") or {}
    steps_out: dict = {}
    if isinstance(steps_in, dict):
        iterator = steps_in.items()
    elif isinstance(steps_in, list):
        # aceita [{name, agent, artifact, next, instructions}, ...] pra facilitar o front
        iterator = [(s.get("name"), s) for s in steps_in if isinstance(s, dict)]
    else:
        iterator = []
    for s_name, s_body in iterator:
        if not isinstance(s_name, str) or not isinstance(s_body, dict):
            continue
        entry: dict = {}
        agent = s_body.get("agent")
        if isinstance(agent, str) and agent.strip():
            entry["agent"] = agent.strip()
        artifact = s_body.get("artifact")
        if isinstance(artifact, str) and artifact.strip():
            entry["artifact"] = artifact.strip()
        next_raw = s_body.get("next") or []
        if not isinstance(next_raw, list):
            next_raw = []
        seen: set[str] = set()
        next_list: list[str] = []
        for n in next_raw:
            if not isinstance(n, str):
                continue
            n = n.strip()
            if not n or n in seen:
                continue
            seen.add(n)
            next_list.append(n)
        entry["next"] = next_list
        instructions = s_body.get("instructions")
        if isinstance(instructions, str):
            cleaned = instructions.replace("\r\n", "\n").rstrip()
            if cleaned.strip():
                if len(cleaned.encode("utf-8")) > 32 * 1024:
                    raise HTTPException(
                        status_code=413,
                        detail=f"step {s_name!r}: instructions excede 32 KB",
                    )
                entry["instructions"] = cleaned
        overrides = _normalize_step_overrides(s_body.get("overrides"))
        if overrides:
            entry["overrides"] = overrides
        if bool(s_body.get("fresh_session", False)):
            entry["fresh_session"] = True
        steps_out[s_name] = entry
    out["steps"] = steps_out
    return out


def _normalize_step_overrides(raw: Any) -> dict:
    """Normaliza o sub-dict `overrides` de um step.

    Whitelist nested: model, effort, memory.{enabled, auto_inject_limit}.
    Drop silencioso de chaves desconhecidas. Strings strip + empty => omitido.
    Numero/bool preservados as-is — validacao semantica em _validate_step_overrides.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    model = raw.get("model")
    if isinstance(model, str) and model.strip():
        out["model"] = model.strip()
    effort = raw.get("effort")
    if isinstance(effort, str) and effort.strip():
        out["effort"] = effort.strip()
    mem_raw = raw.get("memory")
    if isinstance(mem_raw, dict):
        mem_out: dict = {}
        if "enabled" in mem_raw:
            mem_out["enabled"] = mem_raw["enabled"]
        if "auto_inject_limit" in mem_raw:
            mem_out["auto_inject_limit"] = mem_raw["auto_inject_limit"]
        if mem_out:
            out["memory"] = mem_out
    return out


def _validate_workflow_body(name: str, body: dict) -> None:
    """Valida o corpo normalizado. Levanta HTTPException(400) em erro."""
    if not isinstance(name, str) or not _WORKFLOW_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="nome invalido: use minusculas/digitos/-/_ (1-64 chars, comeca com alfanumerico)",
        )
    if name in _WORKFLOW_TERMINALS:
        raise HTTPException(
            status_code=400,
            detail=f"nome colide com terminal reservado do framework: {name}",
        )
    steps = body.get("steps") or {}
    if not isinstance(steps, dict) or not steps:
        raise HTTPException(status_code=400, detail="steps obrigatorio e nao pode ser vazio")
    for s_name in steps.keys():
        if not isinstance(s_name, str) or not _WORKFLOW_NAME_RE.match(s_name):
            raise HTTPException(
                status_code=400,
                detail=f"nome de step invalido: {s_name!r} (use minusculas/digitos/-/_)",
            )
        if s_name in _WORKFLOW_TERMINALS:
            raise HTTPException(
                status_code=400,
                detail=f"nome de step colide com terminal reservado: {s_name}",
            )
    initial = body.get("initial_step") or ""
    if not initial or initial not in steps:
        raise HTTPException(
            status_code=400,
            detail=f"initial_step {initial!r} precisa existir em steps",
        )
    valid_targets = set(steps.keys()) | _WORKFLOW_TERMINALS
    for s_name, s_body in steps.items():
        nxt = s_body.get("next") or []
        if not nxt:
            raise HTTPException(
                status_code=400,
                detail=f"step {s_name!r}: next nao pode ser vazio (use [done]/[halt]/... se for fim)",
            )
        for target in nxt:
            if target not in valid_targets:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"step {s_name!r}: next {target!r} nao existe. "
                        f"Alvos validos: steps declarados ({sorted(steps.keys())}) "
                        f"ou terminais ({sorted(_WORKFLOW_TERMINALS)})."
                    ),
                )
        if "overrides" in s_body:
            _validate_step_overrides(s_name, s_body["overrides"])


def _validate_step_overrides(s_name: str, overrides: Any) -> None:
    """Valida valores semanticos no overrides ja-normalizado. 400 em erro.

    Workflow autoritario: nao confere se model existe no agente nem se
    effort excede teto — runtime resolve. Aqui so checa shape/enum.
    """
    if not isinstance(overrides, dict):
        raise HTTPException(
            status_code=400,
            detail=f"step {s_name!r}: overrides deve ser objeto",
        )
    if "model" in overrides:
        model = overrides["model"]
        if not isinstance(model, str) or not model.strip():
            raise HTTPException(
                status_code=400,
                detail=f"step {s_name!r}: overrides.model deve ser string nao-vazia",
            )
    if "effort" in overrides:
        effort = overrides["effort"]
        if effort not in _WORKFLOW_VALID_EFFORTS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"step {s_name!r}: overrides.effort {effort!r} invalido. "
                    f"Valores aceitos: {list(_WORKFLOW_VALID_EFFORTS)}"
                ),
            )
    if "memory" in overrides:
        mem = overrides["memory"]
        if not isinstance(mem, dict):
            raise HTTPException(
                status_code=400,
                detail=f"step {s_name!r}: overrides.memory deve ser objeto",
            )
        if "enabled" in mem and not isinstance(mem["enabled"], bool):
            raise HTTPException(
                status_code=400,
                detail=f"step {s_name!r}: overrides.memory.enabled deve ser bool",
            )
        if "auto_inject_limit" in mem:
            lim = mem["auto_inject_limit"]
            if not isinstance(lim, int) or isinstance(lim, bool) or lim < 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"step {s_name!r}: overrides.memory.auto_inject_limit deve ser int >= 0",
                )


def _happy_path_order(initial_step: str, steps_raw: dict) -> list[str]:
    """Topo-sort do happy-path: do initial_step, segue o primeiro `next` que
    nao seja terminal, para ao bater terminal ou loop. Ordem da lista `next`
    do yaml e preservada (yaml.safe_load retorna list, nao set).

    Se o grafo diverge (next tem varios steps nao-terminais), pegamos o
    primeiro — e so uma heuristica pra renderizar linear; a autoridade real
    sobre "proximo" continua vindo do agente via complete_phase."""
    ordered: list[str] = []
    visited: set[str] = set()
    current: str | None = initial_step
    while current and current not in visited and current not in _WORKFLOW_TERMINALS:
        if current not in steps_raw:
            break
        visited.add(current)
        ordered.append(current)
        next_list = (steps_raw[current] or {}).get("next") or []
        next_step: str | None = None
        for n in next_list:
            if n not in _WORKFLOW_TERMINALS:
                next_step = n
                break
        current = next_step
    return ordered


def _serialize_workflow(name: str, wf_body: dict) -> dict:
    """Converte o dict lido do yaml no shape que o frontend consome
    (mesmo shape de GET /api/workflows/{name}): inclui steps_ordered."""
    steps_raw = wf_body.get("steps") or {}
    initial_step = wf_body.get("initial_step") or ""
    steps_out: dict[str, dict] = {}
    for s_name, s_body in steps_raw.items():
        if not isinstance(s_body, dict):
            continue
        steps_out[s_name] = {
            "agent": s_body.get("agent"),
            "artifact": s_body.get("artifact"),
            "next": list(s_body.get("next") or []),
            "instructions": s_body.get("instructions"),
            "fresh_session": bool(s_body.get("fresh_session", False)),
        }
    return {
        "name": name,
        "initial_step": initial_step,
        "orchestrator": wf_body.get("orchestrator"),
        "steps_ordered": _happy_path_order(initial_step, steps_raw),
        "steps": steps_out,
        "terminals": sorted(_WORKFLOW_TERMINALS),
        "description": wf_body.get("description"),
    }


@app.get("/api/workflows")
async def workflows_list(expand: int = 0, _: Principal = Depends(get_principal)):
    """Lista workflows. `expand=1` retorna corpo completo (steps+transicoes)
    inline, senao retorna so nomes — padrao antigo preservado pra clientes
    existentes (progress bar)."""
    wfs = _load_workflows_raw()
    if expand:
        items = [_serialize_workflow(n, b) for n, b in wfs.items() if isinstance(b, dict)]
        return {"items": items, "terminals": sorted(_WORKFLOW_TERMINALS), "expanded": True}
    return {"items": list(wfs.keys()), "terminals": sorted(_WORKFLOW_TERMINALS)}


@app.get("/api/workflows/{name}")
async def workflow_get(name: str, _: Principal = Depends(get_principal)):
    """Retorna a definicao do workflow da instancia (sem semantica hardcoded).
    Frontend renderiza progress bar iterando sobre `steps_ordered`.
    Terminais (`done`/`halt`/`human_review`) sao retornados a parte porque
    sao invariantes do framework, nao da instancia."""
    wfs = _load_workflows_raw()
    wf = wfs.get(name)
    if not wf:
        raise HTTPException(status_code=404, detail=f"workflow '{name}' nao declarado em workflows.yaml")
    return _serialize_workflow(name, wf)


@app.put("/api/workflows/{name}")
async def workflow_upsert(
    name: str,
    payload: dict,
    _: Principal = Depends(get_principal),
):
    """Cria ou atualiza um workflow. Body:
      { initial_step: str, steps: {name: {agent?, artifact?, next: [...]}}, description?: str }
    Valida estrutura, escreve no disco. WorkflowRegistry rele a cada
    load(), entao a mudanca vale imediatamente — sem restart/reconcile."""
    normalized = _normalize_workflow_body(payload)
    _validate_workflow_body(name, normalized)
    full = _load_workflows_full()
    wfs = full.get("workflows") or {}
    wfs[name] = normalized
    full["workflows"] = wfs
    size = _write_workflows_yaml(full)
    log.info("workflows.upsert", name=name, size=size)
    return _serialize_workflow(name, normalized)


@app.delete("/api/workflows/{name}")
async def workflow_delete(name: str, _: Principal = Depends(get_principal)):
    full = _load_workflows_full()
    wfs = full.get("workflows") or {}
    if name not in wfs:
        raise HTTPException(status_code=404, detail=f"workflow '{name}' nao existe")
    del wfs[name]
    full["workflows"] = wfs
    size = _write_workflows_yaml(full)
    log.info("workflows.delete", name=name, size=size)
    return {"ok": True}


@app.post("/api/workflows/{name}/rename")
async def workflow_rename(
    name: str,
    payload: dict,
    _: Principal = Depends(get_principal),
):
    new_name = (payload.get("new_name") or "").strip()
    if not _WORKFLOW_NAME_RE.match(new_name):
        raise HTTPException(
            status_code=400,
            detail="new_name invalido: use minusculas/digitos/-/_ (1-64 chars)",
        )
    if new_name in _WORKFLOW_TERMINALS:
        raise HTTPException(status_code=400, detail=f"new_name colide com terminal: {new_name}")
    if new_name == name:
        raise HTTPException(status_code=400, detail="new_name igual ao atual")
    full = _load_workflows_full()
    wfs = full.get("workflows") or {}
    if name not in wfs:
        raise HTTPException(status_code=404, detail=f"workflow '{name}' nao existe")
    if new_name in wfs:
        raise HTTPException(status_code=409, detail=f"workflow '{new_name}' ja existe")
    # Preserva a ordem: substitui a chave in-place em vez de append no final.
    renamed: dict = {}
    for k, v in wfs.items():
        if k == name:
            renamed[new_name] = v
        else:
            renamed[k] = v
    full["workflows"] = renamed
    size = _write_workflows_yaml(full)
    log.info("workflows.rename", old=name, new=new_name, size=size)
    return _serialize_workflow(new_name, renamed[new_name])


# ---------- Backlog (D-53) ----------
# CRUD simples no schema tasks.backlog. Acesso: admin (criacao/edit/promocao
# são acoes humanas) via PWA. Agentes usam via MCP tools backlog_*.

@app.get("/api/backlog")
async def backlog_list(
    status: str | None = None,
    include_all: int = 0,
    _: Principal = Depends(get_principal),
):
    """Lista items do backlog. Default: só status='aberto'. Passe status=X
    pra filtrar ou include_all=1 pra retornar tudo. Ordenado por priority
    desc, updated_at desc.
    """
    if include_all:
        rows = await db.fetch_all(
            """SELECT slug, title, content, priority, impact, effort, status,
                      promoted_task_slug, created_by, created_at, updated_at
                 FROM tasks.backlog
                ORDER BY priority DESC, updated_at DESC""",
        )
    else:
        status = status or "aberto"
        rows = await db.fetch_all(
            """SELECT slug, title, content, priority, impact, effort, status,
                      promoted_task_slug, created_by, created_at, updated_at
                 FROM tasks.backlog
                WHERE status = $1
                ORDER BY priority DESC, updated_at DESC""",
            status,
        )
    return {
        "items": [
            {
                "slug": r["slug"], "title": r["title"], "content": r["content"],
                "priority": int(r["priority"] or 0),
                "impact": r["impact"], "effort": r["effort"],
                "status": r["status"], "promoted_task_slug": r["promoted_task_slug"],
                "created_by": r["created_by"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ]
    }


_BACKLOG_STATUSES = ("aberto", "rascunho", "em_execucao", "promovido", "concluido", "descartado")


@app.post("/api/backlog")
async def backlog_create(payload: dict, principal: Principal = Depends(get_principal)):
    slug = (payload.get("slug") or "").strip()
    title = (payload.get("title") or "").strip()
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="slug invalido (kebab-case)")
    if not title:
        raise HTTPException(status_code=400, detail="title obrigatorio")
    priority = int(payload.get("priority") or 0)
    status = (payload.get("status") or "aberto").strip()
    if status not in _BACKLOG_STATUSES:
        raise HTTPException(status_code=400, detail=f"status invalido: {status!r}")
    await db.execute(
        """INSERT INTO tasks.backlog (slug, title, content, priority, impact, effort, status, created_by)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
           ON CONFLICT (slug) DO UPDATE SET
             title = EXCLUDED.title,
             content = EXCLUDED.content,
             priority = EXCLUDED.priority,
             impact = EXCLUDED.impact,
             effort = EXCLUDED.effort,
             status = EXCLUDED.status""",
        slug, title, payload.get("content") or "",
        priority, payload.get("impact"), payload.get("effort"), status,
        principal.username or "user",
    )
    return {"ok": True, "slug": slug}


@app.patch("/api/backlog/{slug}")
async def backlog_patch(slug: str, payload: dict, _: Principal = Depends(get_principal)):
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="slug invalido")
    if payload.get("status") and payload["status"] not in _BACKLOG_STATUSES:
        raise HTTPException(status_code=400, detail=f"status invalido: {payload['status']!r}")
    fields = []
    params: list = []
    idx = 1
    for col in ("title", "content", "impact", "effort", "status"):
        if col in payload and payload[col] is not None:
            fields.append(f"{col} = ${idx}")
            params.append(payload[col])
            idx += 1
    if "priority" in payload and payload["priority"] is not None:
        fields.append(f"priority = ${idx}")
        params.append(int(payload["priority"]))
        idx += 1
    if not fields:
        raise HTTPException(status_code=400, detail="nenhum campo pra atualizar")
    params.append(slug)
    row = await db.fetch_one(
        f"UPDATE tasks.backlog SET {', '.join(fields)} WHERE slug = ${idx} RETURNING slug, priority, status",
        *params,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"backlog item '{slug}' nao existe")
    return {"ok": True, "slug": row["slug"], "priority": int(row["priority"] or 0), "status": row["status"]}


@app.delete("/api/backlog/{slug}")
async def backlog_delete(slug: str, _: Principal = Depends(get_principal)):
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="slug invalido")
    row = await db.fetch_one(
        "DELETE FROM tasks.backlog WHERE slug = $1 RETURNING slug", slug,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"backlog item '{slug}' nao existe")
    return {"ok": True, "slug": slug, "deleted": True}


@app.post("/api/backlog/{slug}/promote")
async def backlog_promote_endpoint(slug: str, payload: dict, principal: Principal = Depends(get_principal)):
    """Promove item do backlog a task e dispatcha pro primeiro agente.

    Body: { task_slug?, workflow?, next_agent?, initial_topic?, initial_step? }

    Espelha a logica do MCP tool backlog_promote (server.py). Hierarquia
    de convs eh ancorada no `orchestrator` declarado pelo workflow:
    a conv-supervisora vive em `<orchestrator>/task-<slug>` (root) e a
    conv da fase inicial fica como filha (Caminho A) ou unifica com a
    supervisora se `next_agent == orchestrator` (Caminho B). Workflow
    sem `orchestrator` declarado faz fallback pro `next_agent`/`initial_step.agent`
    — comportamento equivalente ao pre-D-110.
    """
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="slug invalido")
    task_slug = (payload.get("task_slug") or slug).strip()
    if not _SLUG_RE.match(task_slug):
        raise HTTPException(status_code=400, detail="task_slug invalido")
    workflow = payload.get("workflow")
    next_agent = payload.get("next_agent")
    initial_topic = (payload.get("initial_topic") or f"task-{task_slug}").strip()
    initial_step = payload.get("initial_step")
    # Resolve initial_step + next_agent default + orchestrator do workflow.
    orchestrator: str | None = None
    if workflow:
        wfs = _load_workflows_raw()
        wf_def = wfs.get(workflow) or {}
        if not initial_step:
            initial_step = (wf_def.get("initial_step")
                            or wf_def.get("first_step")
                            or None)
        if not next_agent and initial_step:
            steps = wf_def.get("steps") or {}
            step_def = steps.get(initial_step) or {}
            next_agent = step_def.get("agent")
        orchestrator = wf_def.get("orchestrator")
    if not next_agent:
        raise HTTPException(
            status_code=400,
            detail="next_agent obrigatorio (passe explicito ou defina workflow com initial_step.agent)",
        )
    # orchestrator: campo do workflow > fallback pro next_agent.
    if not orchestrator:
        orchestrator = next_agent
    origin_stream = orchestrator
    origin_topic = initial_topic
    async with db.connection() as conn:
        async with conn.transaction():
            item = await conn.fetchrow(
                "SELECT title, content FROM tasks.backlog WHERE slug = $1 FOR UPDATE",
                slug,
            )
            if item is None:
                raise HTTPException(status_code=404, detail=f"backlog '{slug}' nao existe")
            existing = await conn.fetchval(
                "SELECT id FROM tasks.tasks WHERE slug = $1", task_slug,
            )
            if existing is None:
                await conn.execute(
                    """INSERT INTO tasks.tasks
                          (slug, title, workflow, status,
                           current_step, current_agent,
                           origin_stream, origin_topic)
                       VALUES ($1, $2, $3, 'in_progress', $4, $5, $6, $7)""",
                    task_slug, item["title"], workflow,
                    initial_step, next_agent,
                    origin_stream, origin_topic,
                )
            await conn.execute(
                """UPDATE tasks.backlog
                      SET status = 'promovido', promoted_task_slug = $2
                    WHERE slug = $1""",
                slug, task_slug,
            )
            payload_ev = {
                "task_slug": task_slug,
                "from_step": None,
                "from_agent": principal.username or "user",
                "artifact": None,
                "summary": f"promovido do backlog: {item['title']}",
                "next": "start",
                "next_agent": next_agent,
                "next_topic": initial_topic,
                "origin_stream": origin_stream,
                "origin_topic": origin_topic,
                "workflow": workflow,
                "backlog_slug": slug,
                "backlog_content": item["content"],
            }
            await conn.execute(
                """INSERT INTO orchestrator.events
                       (emitted_by, event_type, task_slug, payload)
                   VALUES ($1, 'phase_complete', $2, $3::jsonb)""",
                principal.username or "user", task_slug, json.dumps(payload_ev),
            )
    log.info("backlog_promoted",
             slug=slug, task_slug=task_slug,
             orchestrator=orchestrator, next_agent=next_agent)
    return {"ok": True, "backlog_slug": slug, "task_slug": task_slug,
            "orchestrator": orchestrator, "next_agent": next_agent}


@app.post("/api/backlog/{slug}/revert")
async def backlog_revert_endpoint(slug: str, principal: Principal = Depends(get_principal)):
    """Desfaz promocao de um item do backlog. Usado quando o humano
    percebe que despachou pro agente errado e quer re-promover — em
    vez de responder no topic ativo ou rodar SQL na mao.

    Safety gate: recusa se a task ja tem phases completadas OU se ja
    houve mensagem de humano na conv (humano engajou = nao eh erro de
    dispatch, eh trabalho em andamento). Auto-cancela pending_asks,
    apaga conv da task (cascade msgs), deleta task row e volta backlog
    pra status='aberto'. Worktrees so sao deletadas do banco — arquivos
    no filesystem sao responsabilidade do agent (register_worktree nao
    cria automatico).
    """
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="slug invalido")
    async with db.connection() as conn:
        async with conn.transaction():
            bl = await conn.fetchrow(
                "SELECT slug, status, promoted_task_slug FROM tasks.backlog "
                "WHERE slug = $1 FOR UPDATE",
                slug,
            )
            if bl is None:
                raise HTTPException(status_code=404, detail=f"backlog '{slug}' nao existe")
            if bl["status"] != "promovido" or not bl["promoted_task_slug"]:
                raise HTTPException(
                    status_code=409,
                    detail="item nao esta em estado 'promovido' — nada pra desfazer",
                )
            task_slug = bl["promoted_task_slug"]

            task = await conn.fetchrow(
                "SELECT id, status FROM tasks.tasks WHERE slug = $1 FOR UPDATE",
                task_slug,
            )
            if task is not None:
                # Safety: task com phases completas OU com participacao
                # humana nao pode ser revertida sem perda de trabalho.
                phases_done = await conn.fetchval(
                    "SELECT count(*) FROM tasks.phases WHERE task_id = $1 AND completed_at IS NOT NULL",
                    task["id"],
                )
                if phases_done and phases_done > 0:
                    raise HTTPException(
                        status_code=409,
                        detail=f"task ja tem {phases_done} phase(s) completada(s) — nao da pra reverter sem perder trabalho",
                    )
                human_msgs = await conn.fetchval(
                    """SELECT count(*) FROM messaging.messages m
                         JOIN messaging.users u ON u.id = m.sender_id
                         JOIN messaging.conversations c ON c.id = m.conversation_id
                        WHERE c.topic_name = $1
                          AND NOT u.is_bot
                          AND u.username != 'system-bot'
                          AND u.username != 'orchestrator'""",
                    f"task-{task_slug}",
                )
                if human_msgs and human_msgs > 0:
                    raise HTTPException(
                        status_code=409,
                        detail="humano ja postou na conv da task — responda ali em vez de reverter",
                    )

                # Cancela pending_asks de todas as convs da task.
                await conn.execute(
                    """UPDATE messaging.pending_asks pa
                          SET resolved_at = now()
                         WHERE pa.conversation_id IN (
                           SELECT c.id FROM messaging.conversations c
                            WHERE c.topic_name = $1
                         ) AND pa.resolved_at IS NULL""",
                    f"task-{task_slug}",
                )
                # Apaga convs da task (cascade apaga messages + pending_asks).
                await conn.execute(
                    """DELETE FROM messaging.messages
                        WHERE conversation_id IN (
                          SELECT id FROM messaging.conversations
                           WHERE topic_name = $1
                        )""",
                    f"task-{task_slug}",
                )
                await conn.execute(
                    """DELETE FROM messaging.pending_asks
                        WHERE conversation_id IN (
                          SELECT id FROM messaging.conversations
                           WHERE topic_name = $1
                        )""",
                    f"task-{task_slug}",
                )
                await conn.execute(
                    "DELETE FROM messaging.conversations WHERE topic_name = $1",
                    f"task-{task_slug}",
                )
                # Apaga phases/worktrees + task.
                await conn.execute("DELETE FROM tasks.phases WHERE task_id = $1", task["id"])
                await conn.execute("DELETE FROM tasks.worktrees WHERE task_id = $1", task["id"])
                await conn.execute("DELETE FROM tasks.tasks WHERE id = $1", task["id"])

            await conn.execute(
                """UPDATE tasks.backlog
                      SET status = 'aberto', promoted_task_slug = NULL, updated_at = now()
                    WHERE slug = $1""",
                slug,
            )
    log.info("backlog_reverted", slug=slug, task_slug=task_slug, by=principal.username or "user")
    return {"ok": True, "backlog_slug": slug, "task_slug": task_slug}


@app.post("/api/backlog/{slug}/force-reset")
async def backlog_force_reset_endpoint(slug: str, principal: Principal = Depends(get_principal)):
    """D-101: Aniquila uma task em qualquer estado e devolve o item do backlog
    pra 'aberto', pronto pra ser re-promovido. Diferente de /revert, ignora
    safety gates (phases completas, mensagem humana) — uso explicito quando
    o humano quer recomecar do zero apos descobrir falha de design ou
    desviar pra outra abordagem.

    Operacoes (transacao):
      1. Cancela pending_asks, deleta messages/conversations da task
         (topic-name match em todos os streams).
      2. Deleta tasks.phases, tasks.worktrees, tasks.tasks (CASCADE +
         explicit pra resilience).
      3. Purga orchestrator.events com task_slug match.
      4. Apaga company/tasks/<task_slug>/ no filesystem.
      5. Reverte backlog row pra status='aberto', promoted_task_slug=NULL.

    Frontend deve confirmar via modal — endpoint nao tem soft mode."""
    import shutil as _shutil
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="slug invalido")
    async with db.connection() as conn:
        async with conn.transaction():
            bl = await conn.fetchrow(
                "SELECT slug, status, promoted_task_slug FROM tasks.backlog "
                "WHERE slug = $1 FOR UPDATE",
                slug,
            )
            if bl is None:
                raise HTTPException(status_code=404, detail=f"backlog '{slug}' nao existe")
            task_slug = bl["promoted_task_slug"]
            if not task_slug:
                # Item nao foi promovido (ou ja foi resetado antes). Devolve pra
                # 'aberto' como no-op idempotente — operador pode estar limpando
                # leftover de tentativa anterior que falhou no meio.
                await conn.execute(
                    """UPDATE tasks.backlog
                          SET status = 'aberto', promoted_task_slug = NULL, updated_at = now()
                        WHERE slug = $1""",
                    slug,
                )
                log.info("backlog_force_reset_noop", slug=slug, by=principal.username or "user")
                return {"ok": True, "backlog_slug": slug, "task_slug": None, "no_task": True}

            task = await conn.fetchrow(
                "SELECT id FROM tasks.tasks WHERE slug = $1 FOR UPDATE",
                task_slug,
            )

            topic_name = f"task-{task_slug}"
            # Convs cobertas: a task topic em todos os streams + subconvs com
            # parent na conv da task (ask_agent gera __child-* com parent = root).
            await conn.execute(
                """UPDATE messaging.pending_asks pa
                      SET resolved_at = now()
                     WHERE pa.resolved_at IS NULL
                       AND pa.conversation_id IN (
                         SELECT c.id FROM messaging.conversations c
                          WHERE c.topic_name = $1
                             OR c.parent_conv_id IN (
                               SELECT c2.id FROM messaging.conversations c2
                                WHERE c2.topic_name = $1
                             )
                       )""",
                topic_name,
            )
            # CASCADE em messaging.conversations apaga messages e pending_asks
            # filhas; o ON DELETE CASCADE em parent_conv_id (migration 019)
            # remove subconvs ask_agent automaticamente.
            await conn.execute(
                "DELETE FROM messaging.conversations WHERE topic_name = $1",
                topic_name,
            )
            if task is not None:
                await conn.execute("DELETE FROM tasks.phases WHERE task_id = $1", task["id"])
                await conn.execute("DELETE FROM tasks.worktrees WHERE task_id = $1", task["id"])
                await conn.execute("DELETE FROM tasks.tasks WHERE id = $1", task["id"])
            await conn.execute(
                "DELETE FROM orchestrator.events WHERE task_slug = $1",
                task_slug,
            )
            await conn.execute(
                """UPDATE tasks.backlog
                      SET status = 'aberto', promoted_task_slug = NULL, updated_at = now()
                    WHERE slug = $1""",
                slug,
            )
    # Filesystem cleanup fora da transacao DB. Safety: validar prefix.
    artifacts = _TASKS_ARTIFACT_DIR / task_slug
    fs_removed = False
    if artifacts.is_dir() and str(artifacts.resolve()).startswith(
        str(_TASKS_ARTIFACT_DIR.resolve()) + os.sep
    ):
        _shutil.rmtree(artifacts, ignore_errors=True)
        fs_removed = True
    log.info(
        "backlog_force_reset",
        slug=slug, task_slug=task_slug,
        fs_removed=fs_removed,
        by=principal.username or "user",
    )
    return {
        "ok": True,
        "backlog_slug": slug,
        "task_slug": task_slug,
        "fs_removed": fs_removed,
    }


@app.post("/api/backlog/{slug}/reopen")
async def backlog_reopen_endpoint(slug: str, principal: Principal = Depends(get_principal)):
    """Volta item de `descartado` pra `aberto`. Ao contrario de /revert,
    nao ha task/conv/fases pra limpar — descarte nao cria nada. Usado
    quando o humano mudou de ideia depois de descartar uma ideia do
    backlog.

    Aceita apenas status='descartado'. Usa /revert pra promovido→aberto.
    """
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="slug invalido")
    row = await db.fetch_one(
        "SELECT status FROM tasks.backlog WHERE slug = $1",
        slug,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"backlog '{slug}' nao existe")
    if row["status"] != "descartado":
        raise HTTPException(
            status_code=409,
            detail=f"item esta em '{row['status']}', nao em 'descartado' — use /revert pra promovido",
        )
    await db.execute(
        "UPDATE tasks.backlog SET status = 'aberto', updated_at = now() WHERE slug = $1",
        slug,
    )
    log.info("backlog_reopened", slug=slug, by=principal.username or "user")
    return {"ok": True, "slug": slug, "status": "aberto"}


async def _resolve_task_conversation_ids(task_row: dict, phases: list[dict]) -> list[int]:
    """Coleta conversation_ids de TODAS as conversas envolvidas numa task:
      1. origem (onde o humano pediu a task, primeira complete_phase).
      2. next_agent/next_topic de cada orchestrator.event (um por handoff).
      3. terminal stream ($TERMINAL_NOTIFY_STREAM/task-<slug>-final) se configurado.
      4. subconversas de ask_agent (`__ask-from-<agent>-*`) durante a janela da task.

    Usado por /timeline e /stats. Refatorado pra evitar duplicacao.
    """
    slug = task_row["slug"]
    pairs: set[tuple[str, str]] = set()
    if task_row["origin_stream"] and task_row["origin_topic"]:
        pairs.add((task_row["origin_stream"], task_row["origin_topic"]))

    rows = await db.fetch_all(
        "SELECT payload FROM orchestrator.events WHERE task_slug = $1 ORDER BY id",
        slug,
    )
    for r in rows:
        p = r["payload"]
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except Exception:
                continue
        next_agent = p.get("next_agent")
        next_topic = p.get("next_topic")
        if next_agent and next_topic:
            pairs.add((next_agent, next_topic))

    terminal = os.environ.get("TERMINAL_NOTIFY_STREAM", "").strip()
    if terminal:
        pairs.add((terminal, f"task-{slug}-final"))

    agents_in_task = {p["agent"] for p in phases if p.get("agent")}
    if agents_in_task:
        started = [p["started_at"] for p in phases if p.get("started_at")]
        completed = [p["completed_at"] for p in phases if p.get("completed_at")]
        t_start = min(started) if started else None
        t_end = max(completed) if completed else (task_row["updated_at"] or t_start)
        if t_start and t_end:
            from datetime import timedelta as _td
            t_end_slack = t_end + _td(hours=1)
            conds = " OR ".join(
                f"(c.topic_name LIKE ${2*i+3} OR c.topic_name LIKE ${2*i+4})"
                for i in range(len(agents_in_task))
            )
            params: list = [t_start, t_end_slack]
            for a in agents_in_task:
                params.append(f"__ask-from-{a}-%")
                params.append(f"__ask-from-{a}.%")
            ask_rows = await db.fetch_all(
                f"""SELECT DISTINCT s.name AS stream, c.topic_name AS topic
                      FROM messaging.conversations c
                      JOIN messaging.streams s ON s.id = c.stream_id
                     WHERE ({conds})
                       AND c.last_message_at >= $1
                       AND c.last_message_at <= $2""",
                *params,
            )
            for r in ask_rows:
                pairs.add((r["stream"], r["topic"]))

    if not pairs:
        return []

    placeholders = ",".join(f"(${2*i+1}, ${2*i+2})" for i in range(len(pairs)))
    args: list = []
    for s, t in pairs:
        args.extend([s, t])
    convs = await db.fetch_all(
        f"""SELECT c.id
              FROM messaging.conversations c
              JOIN messaging.streams s ON s.id = c.stream_id
             WHERE (s.name, c.topic_name) IN ({placeholders})""",
        *args,
    )
    return [c["id"] for c in convs]


@app.get("/api/tasks/{slug}/stats")
async def task_stats(slug: str, _: Principal = Depends(get_principal)):
    """Agregacao de custo/duracao/turns/runs da task inteira, somando
    telemetry.events (event_type='run_end') de todas as conversations
    envolvidas (mesmo conjunto usado por /timeline).

    Resposta pequena: PWA pode chamar junto com /api/conversations sem dor
    de payload. Agregacao vale pra thread com task — chat livre (sem task)
    cai no calculo local do ConversationPanel baseado em live events.
    """
    task_row = await _fetch_task_row(slug)
    if task_row is None:
        raise HTTPException(status_code=404, detail=f"task '{slug}' nao encontrada")
    phase_rows = await db.fetch_all(
        """SELECT agent, started_at, completed_at
             FROM tasks.phases WHERE task_id = $1 ORDER BY idx ASC""",
        task_row["id"],
    )
    phases = [dict(r) for r in phase_rows]
    conv_ids = await _resolve_task_conversation_ids(task_row, phases)

    # Fonte primaria: telemetry.events via task_slug (migration 019). Sobrevive
    # a delete de conv. `turns` continua vindo de live_events pq o summary nao
    # agrega — mas cai pra 0 se todas as convs foram deletadas (trace some).
    row = await db.fetch_one(
        """SELECT COUNT(*)                                   AS runs,
                  COALESCE(SUM(cost_usd), 0)::float          AS cost_usd,
                  COALESCE(SUM(duration_ms), 0)::bigint      AS duration_ms
             FROM telemetry.events
            WHERE task_slug = $1 AND event_type = 'run_end'""",
        slug,
    )
    turns = 0
    if conv_ids:
        turns_row = await db.fetch_one(
            """SELECT COALESCE(SUM((data->>'num_turns')::int), 0) AS turns
                 FROM telemetry.live_events
                WHERE conversation_id = ANY($1::int[]) AND kind = 'run_end'""",
            conv_ids,
        )
        turns = int(turns_row["turns"] or 0) if turns_row else 0
    return {
        "slug": slug,
        "runs": int(row["runs"] or 0),
        "cost_usd": float(row["cost_usd"] or 0),
        "duration_ms": int(row["duration_ms"] or 0),
        "turns": turns,
        "conversations": len(conv_ids),
    }


@app.get("/api/tasks/{slug}/telemetry")
async def task_telemetry(slug: str, _: Principal = Depends(get_principal)):
    """Breakdown detalhado de telemetria por task (B4 do plano). Sobrevive ao
    delete das conversas da task (via task_slug em telemetry.events, migration
    019). Retorna totais + by_model + by_agent + trace_snapshot agregado das
    rows cujas convs ja foram deletadas."""
    task_row = await _fetch_task_row(slug)
    if task_row is None:
        raise HTTPException(status_code=404, detail=f"task '{slug}' nao encontrada")

    totals_row = await db.fetch_one(
        """SELECT COUNT(*)                                   AS runs,
                  COALESCE(SUM(cost_usd), 0)::float          AS cost_usd,
                  COALESCE(SUM(duration_ms), 0)::bigint      AS duration_ms,
                  COALESCE(SUM(input_tokens), 0)             AS input_tokens,
                  COALESCE(SUM(output_tokens), 0)            AS output_tokens,
                  COALESCE(SUM(cache_creation_tokens), 0) + COALESCE(SUM(cache_read_tokens), 0) AS cache_tokens,
                  MAX(EXTRACT(EPOCH FROM ts))::bigint        AS last_run_ts
             FROM telemetry.events
            WHERE task_slug = $1 AND event_type = 'run_end'""",
        slug,
    )
    by_model_rows = await db.fetch_all(
        """SELECT COALESCE(model, '(unknown)') AS model,
                  COUNT(*)                                   AS runs,
                  COALESCE(SUM(cost_usd), 0)::float          AS cost_usd,
                  COALESCE(AVG(duration_ms)::int, 0)         AS avg_duration_ms
             FROM telemetry.events
            WHERE task_slug = $1 AND event_type = 'run_end'
            GROUP BY COALESCE(model, '(unknown)') ORDER BY cost_usd DESC""",
        slug,
    )
    by_agent_rows = await db.fetch_all(
        """SELECT agent,
                  COUNT(*)                                   AS runs,
                  COALESCE(SUM(cost_usd), 0)::float          AS cost_usd,
                  COALESCE(AVG(duration_ms)::int, 0)         AS avg_duration_ms
             FROM telemetry.events
            WHERE task_slug = $1 AND event_type = 'run_end'
            GROUP BY agent ORDER BY cost_usd DESC""",
        slug,
    )
    # Snapshots de trace pre-delete: somamos o que sobrou em metadata.trace_snapshot
    # pra mostrar "effort" mesmo pos-delete das convs.
    snap_row = await db.fetch_one(
        """SELECT COALESCE(SUM((metadata->'trace_snapshot'->>'thinking_count')::int), 0) AS thinking_count,
                  COALESCE(SUM((metadata->'trace_snapshot'->>'tool_use_count')::int), 0) AS tool_use_count,
                  COUNT(*) FILTER (WHERE metadata ? 'trace_snapshot')                    AS snapshots
             FROM telemetry.events
            WHERE task_slug = $1""",
        slug,
    )
    return {
        "slug": slug,
        "totals": dict(totals_row) if totals_row else {},
        "by_model": [dict(r) for r in by_model_rows],
        "by_agent": [dict(r) for r in by_agent_rows],
        "trace_snapshot": dict(snap_row) if snap_row else {},
    }


@app.get("/api/tasks/{slug}/timeline")
async def task_timeline(slug: str, _: Principal = Depends(get_principal)):
    """Timeline unificada de uma task: mensagens + live events de TODAS as
    conversas envolvidas (origem + cada handoff + terminal stream), sorted
    por timestamp. Pra PWA renderizar tudo num feed so.
    """
    task_row = await _fetch_task_row(slug)
    if task_row is None:
        raise HTTPException(status_code=404, detail=f"task '{slug}' nao encontrada")
    # Carrega phases do banco (schema tasks).
    phase_rows = await db.fetch_all(
        """SELECT step, agent, started_at, completed_at, artifact, summary
             FROM tasks.phases WHERE task_id = $1 ORDER BY idx ASC""",
        task_row["id"],
    )
    phases = [dict(r) for r in phase_rows]
    meta = {
        "slug": task_row["slug"],
        "title": task_row["title"],
        "status": task_row["status"],
        "current_step": task_row["current_step"],
        "current_agent": task_row["current_agent"],
        "origin_stream": task_row["origin_stream"],
        "origin_topic":  task_row["origin_topic"],
        "workflow": task_row["workflow"],
        "updated_at": task_row["updated_at"].isoformat() if task_row["updated_at"] else None,
        "phases": [
            {
                "step": p["step"], "agent": p["agent"],
                "started_at":   p["started_at"].isoformat()   if p["started_at"]   else None,
                "completed_at": p["completed_at"].isoformat() if p["completed_at"] else None,
                "artifact": p["artifact"], "summary": p["summary"],
            } for p in phases
        ],
    }

    # Coleta (stream, topic) envolvidos:
    #   1. origem capturada no banco (primeira complete_phase do coord).
    #   2. next_agent/next_topic de cada orchestrator.event da task.
    #   3. terminal stream (TERMINAL_NOTIFY_STREAM + task-<slug>-final) se configurado.
    pairs: set[tuple[str, str]] = set()
    if task_row["origin_stream"] and task_row["origin_topic"]:
        pairs.add((task_row["origin_stream"], task_row["origin_topic"]))

    rows = await db.fetch_all(
        "SELECT payload FROM orchestrator.events WHERE task_slug = $1 ORDER BY id",
        slug,
    )
    for r in rows:
        p = r["payload"]
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except Exception:
                continue
        next_agent = p.get("next_agent")
        next_topic = p.get("next_topic")
        if next_agent and next_topic:
            pairs.add((next_agent, next_topic))

    terminal = os.environ.get("TERMINAL_NOTIFY_STREAM", "").strip()
    if terminal:
        pairs.add((terminal, f"task-{slug}-final"))

    # 4. ask_agent subconversas: cada agente que PARTICIPOU da task pode ter
    #    feito ask_agent durante sua vez. Topic: `__ask-from-<chain>-<uid>`
    #    onde a chain comeca com o agente asker. Escopo: conversas criadas
    #    durante o window da task (>= primeiro event, <= ultimo + 1h de folga).
    from datetime import datetime as _dt
    agents_in_task = {p["agent"] for p in phases if p.get("agent")}

    if agents_in_task:
        started = [p["started_at"] for p in phases if p.get("started_at")]
        completed = [p["completed_at"] for p in phases if p.get("completed_at")]
        t_start = min(started) if started else None
        t_end = max(completed) if completed else (task_row["updated_at"] or t_start)
        if t_start and t_end:
            conds = " OR ".join(
                f"(c.topic_name LIKE ${2*i+3} OR c.topic_name LIKE ${2*i+4})"
                for i in range(len(agents_in_task))
            )
            from datetime import timedelta as _td
            t_end_slack = t_end + _td(hours=1)
            params: list = [t_start, t_end_slack]
            for a in agents_in_task:
                params.append(f"__ask-from-{a}-%")
                params.append(f"__ask-from-{a}.%")
            ask_rows = await db.fetch_all(
                f"""SELECT DISTINCT s.name AS stream, c.topic_name AS topic
                      FROM messaging.conversations c
                      JOIN messaging.streams s ON s.id = c.stream_id
                     WHERE ({conds})
                       AND c.last_message_at >= $1
                       AND c.last_message_at <= $2""",
                *params,
            )
            for r in ask_rows:
                pairs.add((r["stream"], r["topic"]))

    if not pairs:
        return {"slug": slug, "meta": meta, "conversations": [], "items": []}

    # Resolve conversation_ids.
    placeholders = ",".join(f"(${2*i+1}, ${2*i+2})" for i in range(len(pairs)))
    args: list = []
    for s, t in pairs:
        args.extend([s, t])
    convs = await db.fetch_all(
        f"""SELECT c.id, s.name AS stream, c.topic_name AS topic
              FROM messaging.conversations c
              JOIN messaging.streams s ON s.id = c.stream_id
             WHERE (s.name, c.topic_name) IN ({placeholders})""",
        *args,
    )
    conv_ids = [c["id"] for c in convs]
    convs_info = [{"id": c["id"], "stream": c["stream"], "topic": c["topic"]} for c in convs]

    if not conv_ids:
        return {"slug": slug, "meta": meta, "conversations": [], "items": []}

    # Busca msgs + live_events em todas.
    msgs = await db.fetch_all(
        """SELECT m.id, m.conversation_id, u.username AS sender, u.kind AS sender_kind,
                  m.content, EXTRACT(EPOCH FROM m.sent_at)::bigint AS ts,
                  s.name AS stream, c.topic_name AS topic
             FROM messaging.messages m
             JOIN messaging.users u ON u.id = m.sender_id
             JOIN messaging.conversations c ON c.id = m.conversation_id
             JOIN messaging.streams s ON s.id = c.stream_id
            WHERE m.conversation_id = ANY($1::int[])
            ORDER BY m.id""",
        conv_ids,
    )
    lev = await db.fetch_all(
        """SELECT id, conversation_id, agent, kind, summary, data,
                  EXTRACT(EPOCH FROM ts)::bigint AS ts
             FROM telemetry.live_events
            WHERE conversation_id = ANY($1::int[])
            ORDER BY id""",
        conv_ids,
    )

    items = []
    for m in msgs:
        items.append({
            "kind": "msg",
            "id": f"m{m['id']}",
            "ts": int(m["ts"]),
            "conversation_id": m["conversation_id"],
            "stream": m["stream"],
            "topic": m["topic"],
            "sender": m["sender"],
            "is_bot": m["sender_kind"] == "bot",
            "content": m["content"],
        })
    for e in lev:
        data = e["data"]
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                data = {}
        elif not isinstance(data, dict):
            data = {}
        items.append({
            "kind": "event",
            "id": f"e{e['id']}",
            "ts": int(e["ts"]),
            "conversation_id": e["conversation_id"],
            "agent": e["agent"],
            "event_kind": e["kind"],
            "summary": e["summary"],
            "data": data,
        })
    # Ordena por ts asc; empate: msg antes de event.
    items.sort(key=lambda x: (x["ts"], 0 if x["kind"] == "msg" else 1))

    worktree_rows = await db.fetch_all(
        "SELECT repo, branch, path FROM tasks.worktrees WHERE task_id = $1",
        task_row["id"],
    )
    worktrees = {
        r["repo"]: {"repo": r["repo"], "branch": r["branch"], "path": r["path"]}
        for r in worktree_rows
    }
    return {
        "slug": slug,
        "meta": {
            "title": meta.get("title"),
            "status": meta.get("status"),
            "current_step": meta.get("current_step"),
            "current_agent": meta.get("current_agent"),
            "workflow": meta.get("workflow"),
            "phases": meta.get("phases") or [],
            "worktrees": worktrees,
        },
        "conversations": convs_info,
        "items": items,
    }


# ---------- Health / PWA static ----------

@app.get("/health")
async def health():
    ok = True
    try:
        await db.fetch_one("SELECT 1")
    except Exception:
        ok = False
    return {
        "status": "ok" if ok else "degraded",
        "db": ok,
        "vapid_enabled": bool(os.environ.get("VAPID_PUBLIC_KEY")),
        "transcriber_enabled": bool(os.environ.get("TRANSCRIBER_URL")),
    }


# SvelteKit build output (Phase 6 of big-bang). `static/` (sw.js, manifest,
# icons) e `build/` (SPA gerada pelo Vite) sao servidos lado a lado.
def _index_file() -> Path:
    return BUILD_DIR / "index.html"


# D-96 Cache strategy. Sem isto, FastAPI/Starlette nao envia
# Cache-Control e o browser cacheia via heuristica (10% do age) — em dev
# isso faz o user ver bundles velhos apos rebuild. Estrategia:
#
#   /                       no-cache, must-revalidate  (entry HTML — pequeno,
#                                                       referencia bundles
#                                                       hashed; tem que ser
#                                                       fresco)
#   /sw.js                  no-cache                   (service worker —
#                                                       browser ja faz checagem
#                                                       periodica, mas
#                                                       garante zero stale)
#   /manifest.webmanifest   no-cache                   (depende do .env, pode
#                                                       mudar em runtime)
#   /manifest-icon/*.png    public, max-age=300        (icones mudam raro, 5min
#                                                       de cache eh seguro)
#   /_app/*                 public, max-age=31536000,  (bundles hashed pelo
#                           immutable                   Vite — hash novo = URL
#                                                       novo, conteudo nunca
#                                                       muda no mesmo URL)
#   /static/*               public, max-age=3600       (icons fallback, sw fonte
#                                                       — fresco em 1h)
_NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}
_LONG_CACHE = {"Cache-Control": "public, max-age=300"}
_IMMUTABLE_CACHE = {"Cache-Control": "public, max-age=31536000, immutable"}


@app.get("/", include_in_schema=False)
async def index():
    f = _index_file()
    if not f.exists():
        return JSONResponse({"error": "frontend nao instalado"}, status_code=503)
    return FileResponse(f, media_type="text/html", headers=_NO_CACHE)


@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest():
    """D-94: manifest renderizado dinamicamente do env. Permite que cada
    instancia configure nome/cor/etc sem rebuild da imagem. Defaults
    genericos pra funcionar fora da caixa."""
    body = {
        "id": os.environ.get("PWA_ID", "/agent-framework"),
        "name": os.environ.get("PWA_NAME", "Agents"),
        "short_name": os.environ.get("PWA_SHORT_NAME", "Agents"),
        "description": os.environ.get(
            "PWA_DESCRIPTION", "Multi-agent orchestration"),
        "start_url": os.environ.get("PWA_START_URL", "/"),
        "scope": "/",
        "display": "standalone",
        "display_override": ["window-controls-overlay", "standalone", "minimal-ui"],
        "orientation": os.environ.get("PWA_ORIENTATION", "any"),
        "lang": os.environ.get("PWA_LANG", "en"),
        "categories": ["productivity"],
        "background_color": os.environ.get("PWA_BACKGROUND_COLOR", "#0b1220"),
        "theme_color": os.environ.get("PWA_THEME_COLOR", "#0b1220"),
        "launch_handler": {"client_mode": "focus-existing"},
        "icons": [
            {"src": "/manifest-icon/192.png", "sizes": "192x192",
             "type": "image/png", "purpose": "any"},
            {"src": "/manifest-icon/512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "any"},
        ],
    }
    return JSONResponse(
        body, media_type="application/manifest+json", headers=_NO_CACHE,
    )


@app.get("/manifest-icon/{size}.png", include_in_schema=False)
async def manifest_icon(size: int):
    """D-94: serve icone do PWA — prioriza override por instancia em
    `instance/web/icons/icon-<size>.png`, cai pro default do framework."""
    if size not in (192, 512):
        raise HTTPException(status_code=404, detail="size invalido")
    instance_path = INSTANCE_WEB_DIR / "icons" / f"icon-{size}.png"
    if instance_path.is_file():
        return FileResponse(instance_path, media_type="image/png", headers=_LONG_CACHE)
    return FileResponse(STATIC_DIR / f"icon-{size}.png", media_type="image/png", headers=_LONG_CACHE)


@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    return FileResponse(
        STATIC_DIR / "sw.js", media_type="application/javascript", headers=_NO_CACHE,
    )


# D-96: middleware setta Cache-Control nos mounts /_app e /static que
# StaticFiles do Starlette serve sem header de cache. Bundle em /_app/*
# eh hashed pelo Vite — conteudo no mesmo URL nunca muda, podemos ser
# agressivos (immutable, 1 ano). /static/* contem icone fallback e
# sw fonte — 1h eh seguro.
@app.middleware("http")
async def _cache_control_static(request, call_next):
    response = await call_next(request)
    if "cache-control" in (k.lower() for k in response.headers.keys()):
        return response
    path = request.url.path
    if path.startswith("/_app/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=3600"
    return response


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
if (BUILD_DIR / "_app").is_dir():
    app.mount("/_app", StaticFiles(directory=str(BUILD_DIR / "_app")), name="svelte_app")


# ---------- TTS proxy ----------

@app.post("/api/tts/synthesize")
async def tts_synthesize(payload: dict, _: Principal = Depends(get_principal)):
    """Proxy pra container TTS interno (Piper). Retorna audio/wav."""
    url = os.environ.get("TTS_URL")
    if not url:
        raise HTTPException(status_code=503, detail="TTS nao configurado")
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text obrigatorio")
    if len(text) > 5000:
        raise HTTPException(status_code=413, detail="text > 5000 chars")
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            url.rstrip("/") + "/synthesize",
            json={"text": text, "voice": payload.get("voice")},
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise HTTPException(
                    status_code=502,
                    detail=f"TTS retornou {resp.status}: {body[:200]}",
                )
            data = await resp.read()
    from fastapi.responses import Response as _Response
    return _Response(content=data, media_type="audio/wav")


# ---------- Transcribe preview ----------

@app.post("/api/transcribe-preview")
async def transcribe_preview(
    file: UploadFile = File(...),
    language: str | None = Form(default=None),
):
    url = os.environ.get("TRANSCRIBER_URL")
    if not url:
        raise HTTPException(status_code=503, detail="Transcriber nao configurado")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Arquivo vazio")
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Arquivo > 50MB")
    form = aiohttp.FormData()
    form.add_field("file", content, filename=file.filename or "audio.webm",
                   content_type=file.content_type or "application/octet-stream")
    if language:
        form.add_field("language", language)
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url.rstrip("/") + "/transcribe", data=form) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise HTTPException(status_code=502, detail=f"Transcriber retornou {resp.status}: {body[:200]}")
            return json.loads(body)


# ---------- Push ----------

@app.get("/api/push-config")
async def push_config():
    return {
        "enabled": bool(os.environ.get("VAPID_PUBLIC_KEY")),
        "public_key": os.environ.get("VAPID_PUBLIC_KEY", ""),
    }


@app.post("/api/push/subscribe")
async def push_subscribe(payload: dict, principal: Principal = Depends(get_principal)):
    sub = payload.get("subscription") or payload
    endpoint = sub.get("endpoint")
    keys = sub.get("keys") or {}
    p256dh = keys.get("p256dh")
    auth_key = keys.get("auth")
    if not (endpoint and p256dh and auth_key):
        raise HTTPException(status_code=400, detail="subscription invalida")
    await db.execute(
        """INSERT INTO web.push_subscriptions (endpoint, p256dh, auth, user_id, user_agent)
           VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (endpoint) DO UPDATE SET
             p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth, user_agent = EXCLUDED.user_agent""",
        endpoint, p256dh, auth_key, principal.user_id, payload.get("user_agent"),
    )
    return {"ok": True}


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(payload: dict):
    endpoint = payload.get("endpoint")
    if not endpoint:
        raise HTTPException(status_code=400, detail="endpoint ausente")
    await db.execute("DELETE FROM web.push_subscriptions WHERE endpoint = $1", endpoint)
    return {"ok": True}


@app.post("/api/push/test")
async def push_test(delay_seconds: float = 0.0):
    dispatcher = app.state.push_dispatcher
    if dispatcher is None:
        raise HTTPException(status_code=503, detail="VAPID nao configurado")

    async def _dispatch():
        subs = await db.fetch_all("SELECT endpoint, p256dh, auth FROM web.push_subscriptions")
        sent = 0
        for s in subs:
            try:
                await dispatcher.send_one(endpoint=s["endpoint"], p256dh=s["p256dh"], auth=s["auth"],
                                          title="Ping", body="Push test.", url="/", tag="test")
                sent += 1
            except Exception:
                log.exception("push.test_failed", endpoint=s["endpoint"][:40])
        return sent, len(subs)

    delay = max(0.0, min(60.0, float(delay_seconds)))
    if delay > 0:
        async def _delayed():
            try:
                await asyncio.sleep(delay)
                await _dispatch()
            except Exception:
                log.exception("push.test_delayed_failed")
        asyncio.create_task(_delayed())
        return {"ok": True, "scheduled_in": delay}

    sent, total = await _dispatch()
    return {"ok": True, "sent": sent, "total": total}


# ---------- Telemetry ----------

# ---------- Search global em mensagens ----------

@app.get("/api/search")
async def search_messages(q: str = "", limit: int = 30, _: Principal = Depends(get_principal)):
    """FTS em messaging.messages. Retorna {items: [{stream, topic, conv_id,
    message_id, sender, snippet, ts}]} ordenado por ts_rank desc."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"items": []}
    rows = await db.fetch_all(
        """
        SELECT m.id              AS message_id,
               s.name            AS stream,
               c.topic_name      AS topic,
               c.id              AS conv_id,
               u.username        AS sender,
               m.sent_at         AS ts,
               ts_headline(
                   'simple', m.content,
                   plainto_tsquery('simple', $1),
                   'StartSel=<mark>, StopSel=</mark>, MaxFragments=2, MaxWords=20, MinWords=5'
               )                 AS snippet,
               ts_rank(to_tsvector('simple', m.content),
                       plainto_tsquery('simple', $1)) AS rank
          FROM messaging.messages m
          JOIN messaging.conversations c ON c.id = m.conversation_id
          JOIN messaging.streams s       ON s.id = c.stream_id
          JOIN messaging.users u         ON u.id = m.sender_id
         WHERE to_tsvector('simple', m.content) @@ plainto_tsquery('simple', $1)
         ORDER BY rank DESC, m.sent_at DESC
         LIMIT $2
        """,
        q, min(int(limit), 100),
    )
    return {
        "items": [
            {
                "message_id": r["message_id"],
                "stream": r["stream"],
                "topic": r["topic"],
                "conv_id": f"{r['stream']}/{r['topic']}",
                "sender": r["sender"],
                "snippet": r["snippet"],
                "ts": r["ts"].timestamp(),
            } for r in rows
        ]
    }


# ---------- Live trace ----------
# Agentes postam eventos do stream-json do claude (run_start/thinking/tool_use/
# tool_result/run_end) por conversation. PWA assina via SSE pra ver atividade
# em tempo real. Cleanup via scheduler (cleanup_live_events).

@app.post("/api/telemetry/live-event")
async def telemetry_live_event(payload: dict, principal: Principal = Depends(get_principal)):
    """Aceita {stream, topic, kind, summary?, data?, agent?}. Resolve conv_id
    via stream+topic; INSERT dispara pg_notify pro SSE.

    Dual-write (D-NN, migration 030): mantém messaging.runs em sync com o
    stream de live_events na mesma transaction:
      * run_start  → INSERT row com status='running' (ON CONFLICT bate
                     heartbeat se ja existir running pra mesma conv).
      * run_end    → UPDATE running row pra done/error conforme subtype.
      * qualquer   → bumps last_heartbeat_at na running row (heartbeat
                     piggyback, granularidade fina sem timer separado).
    Se a parte do runs falhar, rollback leva o live_event junto — eh
    aceitavel: fire-and-forget do runner re-emite ou reaper compensa.
    """
    stream = payload.get("stream")
    topic = payload.get("topic")
    kind = payload.get("kind")
    if not (stream and topic and kind):
        raise HTTPException(status_code=400, detail="stream, topic, kind obrigatorios")
    # thinking pode ser multi-paragrafo; cap alto mas existente pra evitar
    # payload absurdo chegando do runner. UI renderiza o texto cheio como bubble.
    summary = payload.get("summary")
    if summary and len(summary) > 8000:
        summary = summary[:7997] + "..."
    # seq_num (migration 018): gerado no agente pra tie-break determinista
    # quando 2 eventos compartilham ts. Opcional — clientes antigos e
    # synthetic events do mcp.server.py nao mandam; fallback pro id via
    # COALESCE nas leituras.
    seq_num = payload.get("seq_num")
    if seq_num is not None:
        try:
            seq_num = int(seq_num)
        except (TypeError, ValueError):
            seq_num = None
    data = payload.get("data") or {}
    agent = payload.get("agent", principal.username)
    async with db.connection() as conn:
        row = await conn.fetchrow(
            """SELECT c.id FROM messaging.conversations c
                 JOIN messaging.streams s ON s.id = c.stream_id
                WHERE s.name = $1 AND c.topic_name = $2""",
            stream, topic,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="conv nao existe")
        conv_id = row["id"]
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO telemetry.live_events
                   (conversation_id, agent, kind, summary, data, seq_num)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                conv_id, agent, kind, summary, json.dumps(data), seq_num,
            )
            await _runs_apply_event(conn, conv_id, agent, stream, topic, kind, data)
    return {"ok": True}


async def _runs_apply_event(
    conn, conv_id: int, agent: str, stream: str, topic: str,
    kind: str, data: dict,
) -> None:
    """Mantem messaging.runs em sync com o evento. Chamado dentro da txn
    do telemetry_live_event handler."""
    if kind == "run_start":
        # Idempotente: se ja ha row 'running' pra essa conv (re-emit do
        # runner ou jitter de POSTs duplicados), so bate o heartbeat.
        # Caso normal: cria nova row 'running'. Reaper transiciona pra
        # 'stale' caso o run anterior tenha morrido sem run_end — quando
        # isso ocorre, o INSERT abaixo nao bate o partial unique index
        # (que so cobre status='running') e cria row nova como esperado.
        await conn.execute(
            """INSERT INTO messaging.runs
                 (conversation_id, agent, topic_slug, status, metadata)
               VALUES ($1, $2, $3, 'running', $4::jsonb)
               ON CONFLICT (conversation_id) WHERE status = 'running'
               DO UPDATE SET last_heartbeat_at = now()""",
            conv_id, agent, topic,
            json.dumps({"start_summary": data} if data else {}),
        )
    elif kind == "run_end":
        # Convencao Claude CLI: subtype None ou 'success' = ok; senao erro.
        # synthetic run_end (D-71, claude_runner.py:1290) tambem manda
        # subtype; tratamos igual.
        subtype = data.get("subtype") if isinstance(data, dict) else None
        new_status = "done" if subtype in (None, "success") else "error"
        # Update the most recent running row pra essa conv. Subquery por
        # PK pra UPDATE conseguir ORDER BY/LIMIT.
        await conn.execute(
            """UPDATE messaging.runs
                  SET status = $2,
                      finished_at = now(),
                      exit_reason = $3,
                      last_heartbeat_at = now()
                WHERE id = (
                    SELECT id FROM messaging.runs
                     WHERE conversation_id = $1 AND status = 'running'
                     ORDER BY started_at DESC LIMIT 1
                )""",
            conv_id, new_status, subtype,
        )
    else:
        # thinking / tool_use / tool_result / etc → heartbeat free.
        # No-op se nao houver row 'running' (defensivo; pode acontecer se
        # run_start foi perdido no fire-and-forget).
        await conn.execute(
            """UPDATE messaging.runs
                  SET last_heartbeat_at = now()
                WHERE conversation_id = $1 AND status = 'running'""",
            conv_id,
        )


@app.get("/api/conversations/{conv_id:path}/live/recent")
async def conversation_live_recent(conv_id: str, limit: int = 50, _: Principal = Depends(get_principal)):
    numeric_id = await _resolve_conv_id(conv_id)
    rows = await db.fetch_all(
        """SELECT id, agent, kind, summary, ts, data,
                  COALESCE(seq_num, id) AS seq_num
             FROM telemetry.live_events
            WHERE conversation_id = $1
            ORDER BY id DESC
            LIMIT $2""",
        numeric_id, min(int(limit), 500),
    )
    items = []
    for r in reversed(rows):
        data = r["data"]
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                data = {}
        elif not isinstance(data, dict):
            data = {}
        items.append({
            "id": r["id"], "agent": r["agent"], "kind": r["kind"],
            "summary": r["summary"], "ts": r["ts"].timestamp(),
            "data": data, "seq_num": r["seq_num"],
        })
    return {"items": items}


@app.get("/api/conversations/{conv_id:path}/live/stream")
async def conversation_live_stream(conv_id: str, _: Principal = Depends(get_principal)):
    """SSE LISTEN no channel `live_event_<conv_id>`. Conexao fica aberta
    enquanto cliente nao desconecta. Heartbeat a cada 25s pra evitar idle close."""
    numeric_id = await _resolve_conv_id(conv_id)
    channel = f"live_event_{numeric_id}"
    dsn = os.environ["DATABASE_URL"]

    async def event_generator():
        conn = await asyncpg.connect(dsn)
        queue: asyncio.Queue[str] = asyncio.Queue()

        def _cb(_c, _pid, _ch, payload):
            queue.put_nowait(payload)

        await conn.add_listener(channel, _cb)
        # Sinaliza ready ao cliente
        yield f": connected to {channel}\n\n"
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=25.0)
                    # Trigger envia so id/agent/ts/kind/summary (cabe no limite
                    # 8KB do pg_notify). Front precisa de `data` (tool name,
                    # input) pra renderizar tool_use sem fallback '?'. Hidrata
                    # via lookup; +1 query por evento, mas SSE de live events
                    # e baixa frequencia.
                    try:
                        evt = json.loads(payload)
                        row = await conn.fetchrow(
                            "SELECT data, COALESCE(seq_num, id) AS seq_num "
                            "FROM telemetry.live_events WHERE id = $1",
                            evt.get("id"),
                        )
                        if row is not None:
                            data = row["data"]
                            if isinstance(data, str):
                                try:
                                    data = json.loads(data)
                                except Exception:
                                    data = {}
                            elif not isinstance(data, dict):
                                data = {}
                            evt["data"] = data
                            evt["seq_num"] = row["seq_num"]
                            payload = json.dumps(evt)
                    except Exception:
                        # Fallback: envia payload bruto do trigger se hidratacao
                        # falhar (row sumiu, JSON invalido, etc).
                        pass
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    # Heartbeat-comment SSE
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await conn.remove_listener(channel, _cb)
                await conn.close()
            except Exception:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/live_events/{event_id}/full")
async def live_event_full(event_id: int, _: Principal = Depends(get_principal)):
    """Retorna o `data` completo de um live_event (incluindo `output_full`
    que o trigger strip-a do NOTIFY pra caber em 8KB). Usado pelo PWA
    quando o usuario clica em "View full" num tool_result truncado.
    JSONB no banco nao tem cap pratico — limite real eh o
    OUTPUT_FULL_CAP do claude_runner (~200KB)."""
    row = await db.fetch_one(
        "SELECT id, agent, kind, summary, ts, data FROM telemetry.live_events WHERE id = $1",
        event_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="live_event nao existe")
    data = row["data"]
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    elif not isinstance(data, dict):
        data = {}
    return {
        "id": row["id"],
        "agent": row["agent"],
        "kind": row["kind"],
        "summary": row["summary"],
        "ts": row["ts"].timestamp() if row["ts"] else None,
        "data": data,
    }


@app.get("/api/tasks/events")
async def tasks_events_stream(_: Principal = Depends(get_principal)):
    """SSE LISTEN no channel `task_changed` (trigger `tasks.tasks` AFTER
    INSERT/UPDATE, migration 012). Payload slim: slug+status+current_agent.
    Consumer no PWA faz refetch da lista — derivacoes (phases_count etc)
    vem do endpoint REST que ja existe."""
    dsn = os.environ["DATABASE_URL"]

    async def event_generator():
        conn = await asyncpg.connect(dsn)
        queue: asyncio.Queue[str] = asyncio.Queue()

        def _cb(_c, _pid, _ch, payload):
            queue.put_nowait(payload)

        await conn.add_listener("task_changed", _cb)
        yield ": connected to task_changed\n\n"
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await conn.remove_listener("task_changed", _cb)
                await conn.close()
            except Exception:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/scheduler/events")
async def scheduler_events_stream(_: Principal = Depends(get_principal)):
    """SSE LISTEN no channel `scheduler_event`. Emitido pelo container
    scheduler via `_pg_notify` (orchestrator/scheduler.py) ao final de cada
    dispatch_job — payload contem job_id, action, status, last_fire_at."""
    dsn = os.environ["DATABASE_URL"]

    async def event_generator():
        conn = await asyncpg.connect(dsn)
        queue: asyncio.Queue[str] = asyncio.Queue()

        def _cb(_c, _pid, _ch, payload):
            queue.put_nowait(payload)

        await conn.add_listener("scheduler_event", _cb)
        yield ": connected to scheduler_event\n\n"
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await conn.remove_listener("scheduler_event", _cb)
                await conn.close()
            except Exception:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/telemetry/event")
async def telemetry_event(payload: dict, principal: Principal = Depends(get_principal)):
    # task_slug: runner envia explicito. Fallback server-side pra telemetria
    # de clientes antigos: topic_slug vem como '<stream>__<topic>', parte 2
    # costuma ser o topic_name; se comeca com 'task-', extrai o slug.
    task_slug = payload.get("task_slug")
    if not task_slug:
        topic = payload.get("topic_slug") or ""
        topic_name = topic.split("__", 1)[1] if "__" in topic else topic
        if topic_name.startswith("task-"):
            task_slug = topic_name[5:]
    # conversation_id: runner novo envia stream+topic; resolve via mesma
    # pratica do /live-event. Best-effort — se a conv nao existir mais (ex:
    # run emite telemetry apos delete), fica NULL. Tambem aceita
    # conversation_id explicito no payload (testes/migration backfill).
    conversation_id = payload.get("conversation_id")
    if conversation_id is None:
        stream = payload.get("stream")
        topic_name = payload.get("topic")
        if stream and topic_name:
            row = await db.fetch_one(
                """SELECT c.id FROM messaging.conversations c
                     JOIN messaging.streams s ON s.id = c.stream_id
                    WHERE s.name = $1 AND c.topic_name = $2""",
                stream, topic_name,
            )
            if row is not None:
                conversation_id = row["id"]
    # Fallback final pra task_slug quando topic nao tem prefixo `task-`
    # (caso do mega-agente: ops opera no topic original do humano,
    # ex: `2026-05-05 15:16`, sem prefixo). Se conversation_id resolveu,
    # cruza com tasks.tasks por origin_stream/origin_topic — pega a task
    # mais recente com aquela origem. Para o multi-agente classico esse
    # branch nao executa porque o prefixo `task-` ja resolveu task_slug
    # acima. Edge: se varias tasks compartilham a mesma origem (mesma conv
    # rodou X depois Y), atribui pra Y (mais recente) — comportamento
    # desejado ("task atualmente ativa nesta conv").
    if not task_slug and conversation_id is not None:
        row = await db.fetch_one(
            """SELECT t.slug
                 FROM tasks.tasks t
                 JOIN messaging.streams s ON s.name = t.origin_stream
                 JOIN messaging.conversations c
                   ON c.stream_id = s.id AND c.topic_name = t.origin_topic
                WHERE c.id = $1
                ORDER BY t.updated_at DESC
                LIMIT 1""",
            conversation_id,
        )
        if row is not None:
            task_slug = row["slug"]
    await db.execute(
        """INSERT INTO telemetry.events
           (agent, topic_slug, event_type, cost_usd,
            input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
            duration_ms, model, metadata, conversation_id, task_slug)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)""",
        payload.get("agent", principal.username),
        payload.get("topic_slug"),
        payload.get("event_type", "unknown"),
        payload.get("cost_usd"),
        payload.get("input_tokens"),
        payload.get("output_tokens"),
        payload.get("cache_creation_tokens"),
        payload.get("cache_read_tokens"),
        payload.get("duration_ms"),
        payload.get("model"),
        json.dumps(payload.get("metadata", {})),
        conversation_id,
        task_slug,
    )
    return {"ok": True}


@app.get("/api/telemetry/summary")
async def telemetry_summary(
    window: str = "24h",
    agent: str | None = None,
    model: str | None = None,
):
    window_map = {"1h": "1 hour", "24h": "24 hours", "7d": "7 days", "30d": "30 days"}
    interval = window_map.get(window, "24 hours")
    # Os 3 contadores de input sao disjuntos (semantica Anthropic): input_tokens
    # = nao-cacheado, cache_creation = escrito no cache, cache_read = lido do cache.
    # Cada um com preco proprio. Expomos os 3 somados no window + um `cache_tokens`
    # derivado (creation+read) pro breakdown continuar facil no PWA.
    filters = [f"ts > now() - INTERVAL '{interval}'"]
    params: list = []
    if agent:
        params.append(agent)
        filters.append(f"agent = ${len(params)}")
    if model:
        params.append(model)
        filters.append(f"model = ${len(params)}")
    where_sql = " AND ".join(filters)

    by_agent_rows = await db.fetch_all(
        f"""SELECT agent,
               COUNT(*) AS runs,
               COALESCE(SUM(cost_usd), 0)::float AS cost_usd,
               COALESCE(AVG(duration_ms)::int, 0) AS avg_duration_ms,
               COALESCE(SUM(input_tokens), 0) AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COALESCE(SUM(cache_creation_tokens), 0) + COALESCE(SUM(cache_read_tokens), 0) AS cache_tokens,
               COUNT(*) FILTER (WHERE event_type LIKE '%error%') AS failures
             FROM telemetry.events
            WHERE {where_sql}
            GROUP BY agent ORDER BY cost_usd DESC""",
        *params,
    )
    by_model_rows = await db.fetch_all(
        f"""SELECT COALESCE(model, '(unknown)') AS model,
               COUNT(*) AS runs,
               COALESCE(SUM(cost_usd), 0)::float AS cost_usd,
               COALESCE(AVG(duration_ms)::int, 0) AS avg_duration_ms,
               COALESCE(SUM(input_tokens), 0) AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COALESCE(SUM(cache_creation_tokens), 0) + COALESCE(SUM(cache_read_tokens), 0) AS cache_tokens,
               COUNT(*) FILTER (WHERE event_type LIKE '%error%') AS failures
             FROM telemetry.events
            WHERE {where_sql}
            GROUP BY COALESCE(model, '(unknown)') ORDER BY cost_usd DESC""",
        *params,
    )
    totals_row = await db.fetch_one(
        f"""SELECT COUNT(*) AS runs,
                   COALESCE(SUM(cost_usd), 0)::float AS total_cost_usd,
                   COALESCE(SUM(duration_ms), 0) AS total_duration_ms,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(cache_creation_tokens), 0) + COALESCE(SUM(cache_read_tokens), 0) AS cache_tokens
              FROM telemetry.events WHERE {where_sql}""",
        *params,
    )
    return {
        "window": window,
        "filters": {"agent": agent, "model": model},
        "totals": dict(totals_row) if totals_row else {},
        "by_agent": [dict(r) for r in by_agent_rows],
        "by_model": [dict(r) for r in by_model_rows],
    }


@app.get("/api/telemetry/timeseries")
async def telemetry_timeseries(
    window: str = "24h",
    agent: str | None = None,
    model: str | None = None,
):
    """Series agregadas pra grafico time-series. Bucket tamanho automatico."""
    cfg = {
        "1h":  ("1 hour",   "5 minutes"),
        "24h": ("24 hours", "1 hour"),
        "7d":  ("7 days",   "6 hours"),
        "30d": ("30 days",  "1 day"),
    }
    interval, bucket = cfg.get(window, cfg["24h"])
    filters = [f"ts > now() - INTERVAL '{interval}'"]
    params: list = []
    if agent:
        params.append(agent)
        filters.append(f"agent = ${len(params)}")
    if model:
        params.append(model)
        filters.append(f"model = ${len(params)}")
    where_sql = " AND ".join(filters)
    rows = await db.fetch_all(
        f"""SELECT date_bin('{bucket}', ts, TIMESTAMP '2000-01-01')::timestamptz AS bucket,
                   COUNT(*) AS runs,
                   COALESCE(SUM(cost_usd), 0)::float AS cost_usd,
                   COALESCE(SUM(duration_ms), 0)::bigint AS duration_ms,
                   COALESCE(SUM(output_tokens), 0)::bigint AS output_tokens
              FROM telemetry.events
             WHERE {where_sql}
             GROUP BY bucket
             ORDER BY bucket""",
        *params,
    )
    return {
        "window": window,
        "bucket": bucket,
        "points": [
            {
                "ts": int(r["bucket"].timestamp()),
                "runs": int(r["runs"]),
                "cost_usd": float(r["cost_usd"] or 0),
                "duration_ms": int(r["duration_ms"] or 0),
                "output_tokens": int(r["output_tokens"] or 0),
            } for r in rows
        ],
    }


@app.get("/api/telemetry/recent")
async def telemetry_recent(
    limit: int = 50,
    agent: str | None = None,
    model: str | None = None,
):
    filters: list[str] = []
    params: list = []
    if agent:
        params.append(agent)
        filters.append(f"agent = ${len(params)}")
    if model:
        params.append(model)
        filters.append(f"model = ${len(params)}")
    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    params.append(min(limit, 500))
    rows = await db.fetch_all(
        f"""SELECT * FROM telemetry.events {where_sql}
             ORDER BY ts DESC LIMIT ${len(params)}""",
        *params,
    )
    import json as _json
    items = []
    for r in rows:
        meta = r["metadata"]
        if isinstance(meta, str):
            try:
                meta = _json.loads(meta)
            except Exception:
                meta = {}
        elif not isinstance(meta, dict):
            meta = {}
        items.append({
            "id": r["id"], "agent": r["agent"], "topic_slug": r["topic_slug"],
            "event_type": r["event_type"],
            "cost_usd": float(r["cost_usd"] or 0),
            "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
            "duration_ms": r["duration_ms"], "model": r["model"],
            "started_at": int(r["ts"].timestamp()),
            "ts": r["ts"].isoformat(), "metadata": meta,
            "num_turns": meta.get("num_turns"),
            "exit_code": meta.get("exit_code"),
        })
    return {"items": items}


# ---------- Company context + philosophy + agent policies ----------

COMPANY_CONTEXT_PATH = Path("/workspace/company/CONTEXT.md")
COMPANY_PHILOSOPHY_PATH = Path("/workspace/company/philosophy.md")
COMPANY_FILE_MAX_BYTES = 64 * 1024  # cap 64KB cada (system prompt enxuga depois)


def _read_company_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except Exception as e:
        log.exception("company.read_failed", path=str(path), err=str(e))
        return ""


def _write_company_file(path: Path, content: str) -> int:
    if len(content.encode("utf-8")) > COMPANY_FILE_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"content > {COMPANY_FILE_MAX_BYTES} bytes")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path.stat().st_size


@app.get("/api/company/context")
async def company_context_get(_: Principal = Depends(get_principal)):
    return {"path": "company/CONTEXT.md", "content": _read_company_file(COMPANY_CONTEXT_PATH)}


@app.post("/api/company/context")
async def company_context_set(payload: dict, _: Principal = Depends(get_principal)):
    content = payload.get("content", "")
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="content deve ser string")
    size = _write_company_file(COMPANY_CONTEXT_PATH, content)
    return {"ok": True, "size": size}


@app.get("/api/company/philosophy")
async def company_philosophy_get(_: Principal = Depends(get_principal)):
    return {"path": "company/philosophy.md", "content": _read_company_file(COMPANY_PHILOSOPHY_PATH)}


@app.post("/api/company/philosophy")
async def company_philosophy_set(payload: dict, _: Principal = Depends(get_principal)):
    content = payload.get("content", "")
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="content deve ser string")
    size = _write_company_file(COMPANY_PHILOSOPHY_PATH, content)
    return {"ok": True, "size": size}


PHILOSOPHIES_TEMPLATES_DIR = Path("/app/templates/philosophies")


@app.get("/api/philosophies/templates")
async def list_philosophy_templates(_: Principal = Depends(get_principal)):
    """Lista templates de filosofia disponiveis (framework/templates/philosophies/)."""
    if not PHILOSOPHIES_TEMPLATES_DIR.is_dir():
        return {"items": []}
    items = []
    for f in sorted(PHILOSOPHIES_TEMPLATES_DIR.glob("*.md")):
        if f.name.startswith("_"):
            continue
        try:
            text = f.read_text(encoding="utf-8")
            # extrai title da 1a linha "# X" e summary do bloco
            lines = text.splitlines()
            title = lines[0].lstrip("# ").strip() if lines else f.stem
            summary = ""
            for line in lines[:5]:
                if "summary:" in line.lower():
                    summary = line.split("summary:", 1)[1].strip(" *_")
                    break
            items.append({
                "slug": f.stem,
                "title": title,
                "summary": summary,
                "content": text,
            })
        except Exception:
            log.exception("philosophy_template.read_failed", file=str(f))
    return {"items": items}


@app.get("/api/agents")
async def list_agents_meta(_: Principal = Depends(get_principal)):
    """Lista agentes ativos (kind=bot, is_active=true) com metadata pra UI
    de policies/onboard. Bots desativados via reconcile/soft-delete (agente
    saiu do agents.yaml) ficam fora — convs antigas continuam acessiveis
    mas o agente nao reaparece como peer chamavel."""
    rows = await db.fetch_all(
        """SELECT u.username, u.full_name, u.agent_name
             FROM messaging.users u
            WHERE u.kind = 'bot' AND u.agent_name IS NOT NULL
              AND u.is_active = true
            ORDER BY u.agent_name"""
    )
    return {
        "items": [
            {
                "name": r["agent_name"],
                "display_name": r["full_name"],
                "username": r["username"],
            } for r in rows
        ]
    }


@app.get("/api/agent-policies")
async def list_agent_policies(_: Principal = Depends(get_principal)):
    rows = await db.fetch_all(
        "SELECT agent, can_ask, can_be_asked_by, updated_at FROM messaging.agent_policies ORDER BY agent"
    )
    return {
        "items": [
            {
                "agent": r["agent"],
                "can_ask": r["can_ask"],
                "can_be_asked_by": r["can_be_asked_by"],
                "updated_at": r["updated_at"].isoformat(),
            } for r in rows
        ]
    }


@app.post("/api/agent-policies")
async def upsert_agent_policy(payload: dict, _: Principal = Depends(get_principal)):
    """Upsert policy. Body: {agent, can_ask?, can_be_asked_by?}.
    can_ask=null OR ausente => sem restricao (pode pedir a qualquer um).
    can_ask=[] => bloqueado de pedir a qualquer um.
    can_ask=['x','y'] => whitelist."""
    agent = (payload.get("agent") or "").strip()
    if not agent:
        raise HTTPException(status_code=400, detail="agent obrigatorio")

    def _norm(v):
        if v is None:
            return None
        if not isinstance(v, list):
            raise HTTPException(status_code=400, detail="can_ask/can_be_asked_by devem ser listas ou null")
        return [str(x).strip() for x in v if str(x).strip()]

    can_ask = _norm(payload.get("can_ask"))
    can_be_asked_by = _norm(payload.get("can_be_asked_by"))
    await db.execute(
        """INSERT INTO messaging.agent_policies (agent, can_ask, can_be_asked_by)
           VALUES ($1, $2, $3)
           ON CONFLICT (agent) DO UPDATE SET
             can_ask = EXCLUDED.can_ask,
             can_be_asked_by = EXCLUDED.can_be_asked_by,
             updated_at = now()""",
        agent, can_ask, can_be_asked_by,
    )
    return {"ok": True, "agent": agent}


@app.delete("/api/agent-policies/{agent}")
async def delete_agent_policy(agent: str, _: Principal = Depends(get_principal)):
    await db.execute("DELETE FROM messaging.agent_policies WHERE agent = $1", agent)
    return {"ok": True}


# ---------- System prompts (D-63) ----------
# Tudo que vai pro --append-system-prompt do `claude -p` mora aqui — editavel
# pelo PWA, lido a cada invocacao por claude_runner (sem restart).

import yaml  # noqa: E402

SYSTEM_PROMPTS_DIR = Path("/workspace/company/system_prompts")
PLATFORM_PROMPT_PATH = SYSTEM_PROMPTS_DIR / "platform.md"
SYSTEM_PROMPTS_CONFIG_PATH = SYSTEM_PROMPTS_DIR / "config.yaml"
# No container web, AGENTS_DIR e bind-montado em /workspace/agents (vs /app/agents
# nos containers de agente — paths diferentes por container, propositais).
AGENTS_CONTAINER_DIR = Path("/workspace/agents")

SYSTEM_PROMPT_SECTION_FILE = 64 * 1024  # cap por arquivo (igual CONTEXT)

# Defaults aplicados se chave faltar no config.yaml — espelha claude_runner.
SYSTEM_PROMPT_TOGGLE_DEFAULTS: dict[str, bool] = {
    "include_platform_prompt": True,
    "include_company_context": True,
    "include_company_philosophy": True,
    "include_agent_claude_md": True,
    "include_team_block": True,
    # Blocos dinamicos contextuais (D-110+):
    "include_invocation_context": True,
    "include_task_state": True,
    "include_step_instructions": True,
}


def _read_system_prompt_config() -> dict[str, bool]:
    toggles = dict(SYSTEM_PROMPT_TOGGLE_DEFAULTS)
    try:
        raw = SYSTEM_PROMPTS_CONFIG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return toggles
    except Exception:
        log.exception("system_prompt.config_read_failed")
        return toggles
    try:
        data = yaml.safe_load(raw) or {}
    except Exception:
        log.exception("system_prompt.config_parse_failed")
        return toggles
    if isinstance(data, dict):
        for key in toggles:
            v = data.get(key)
            if isinstance(v, bool):
                toggles[key] = v
    return toggles


def _write_system_prompt_config(toggles: dict[str, bool]) -> None:
    SYSTEM_PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    header = (
        "# system_prompts/config.yaml — toggles globais do system prompt.\n"
        "# Editado pelo PWA (Empresa > System Prompts). claude_runner re-le a\n"
        "# cada invocacao — mudancas valem na proxima task, sem restart.\n\n"
    )
    body = yaml.safe_dump(toggles, default_flow_style=False, sort_keys=False)
    SYSTEM_PROMPTS_CONFIG_PATH.write_text(header + body, encoding="utf-8")


def _agent_claude_md_path(agent: str) -> Path:
    # Valida nome (mesma regex do reconcile NAME_RE) pra evitar path traversal.
    import re
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", agent):
        raise HTTPException(status_code=400, detail=f"agent name invalido: {agent!r}")
    return AGENTS_CONTAINER_DIR / agent / "CLAUDE.md"


def _read_section(key: str) -> tuple[Path, str]:
    """Resolve a section key para (path, conteudo). Suporta `agent:<name>`."""
    if key == "platform":
        path = PLATFORM_PROMPT_PATH
    elif key == "context":
        path = COMPANY_CONTEXT_PATH
    elif key == "philosophy":
        path = COMPANY_PHILOSOPHY_PATH
    elif key.startswith("agent:"):
        path = _agent_claude_md_path(key.split(":", 1)[1])
    else:
        raise HTTPException(status_code=404, detail=f"section desconhecida: {key!r}")
    try:
        return path, path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return path, ""
    except Exception as e:
        log.exception("system_prompt.section_read_failed", section=key)
        raise HTTPException(status_code=500, detail=f"read failed: {e}")


def _write_section(key: str, content: str) -> int:
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="content deve ser string")
    if len(content.encode("utf-8")) > SYSTEM_PROMPT_SECTION_FILE:
        raise HTTPException(
            status_code=413,
            detail=f"content > {SYSTEM_PROMPT_SECTION_FILE} bytes",
        )
    path, _ = _read_section(key)  # valida key (e checa traversal pra agent:)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path.stat().st_size


@app.get("/api/system-prompts")
async def system_prompts_index(_: Principal = Depends(get_principal)):
    """Lista metadata das secoes editaveis + toggles globais.

    Cada secao: key, title, source_path (relativo a /workspace), present, size,
    toggle_key (chave em config.yaml que liga/desliga essa secao), generated
    (true se o conteudo eh montado dinamicamente, sem arquivo)."""
    toggles = _read_system_prompt_config()
    sections: list[dict] = []
    fixed = [
        ("platform", "Plataforma (regras invariantes)", PLATFORM_PROMPT_PATH,
         "include_platform_prompt", False),
        ("context", "Contexto da empresa", COMPANY_CONTEXT_PATH,
         "include_company_context", False),
        ("philosophy", "Filosofia operacional", COMPANY_PHILOSOPHY_PATH,
         "include_company_philosophy", False),
    ]
    for key, title, path, toggle_key, generated in fixed:
        st = path.stat() if path.exists() else None
        sections.append({
            "key": key,
            "title": title,
            "source_path": str(path).replace("/workspace/", ""),
            "present": st is not None,
            "size": st.st_size if st else 0,
            "toggle_key": toggle_key,
            "enabled": toggles.get(toggle_key, True),
            "generated": generated,
        })
    # Blocos dinamicos contextuais (D-110+) — gerados a cada spawn.
    dynamic_blocks = [
        ("invocation_context",
         "Modo de invocacao (raiz vs filha)",
         "include_invocation_context"),
        ("task_state",
         "Estado da task (snapshot quando topic = task-*)",
         "include_task_state"),
        ("step_instructions",
         "Instrucoes da fase atual (workflows.yaml.steps.<step>.instructions)",
         "include_step_instructions"),
    ]
    for key, title, toggle_key in dynamic_blocks:
        sections.append({
            "key": key,
            "title": title,
            "source_path": None,
            "present": True,
            "size": 0,
            "toggle_key": toggle_key,
            "enabled": toggles.get(toggle_key, True),
            "generated": True,
        })
    # Bloco Equipe — gerado, sem arquivo.
    sections.append({
        "key": "team",
        "title": "Equipe (gerado dinamicamente do banco)",
        "source_path": None,
        "present": True,
        "size": 0,
        "toggle_key": "include_team_block",
        "enabled": toggles.get("include_team_block", True),
        "generated": True,
    })
    # Por-agente: 1 entry por agente bot ativo (is_active=true).
    rows = await db.fetch_all(
        """SELECT u.agent_name AS name, u.full_name AS display_name
             FROM messaging.users u
            WHERE u.kind = 'bot' AND u.agent_name IS NOT NULL
              AND u.is_active = true
            ORDER BY u.agent_name"""
    )
    agents = []
    for r in rows:
        path = _agent_claude_md_path(r["name"])
        st = path.stat() if path.exists() else None
        agents.append({
            "key": f"agent:{r['name']}",
            "name": r["name"],
            "display_name": r["display_name"],
            "title": f"CLAUDE.md — {r['display_name']} ({r['name']})",
            "source_path": f"agents/{r['name']}/CLAUDE.md",
            "present": st is not None,
            "size": st.st_size if st else 0,
            "toggle_key": "include_agent_claude_md",
            "enabled": toggles.get("include_agent_claude_md", True),
            "generated": False,
        })
    return {
        "toggles": toggles,
        "sections": sections,
        "agents": agents,
    }


@app.get("/api/system-prompts/config")
async def system_prompts_config_get(_: Principal = Depends(get_principal)):
    return {"toggles": _read_system_prompt_config()}


@app.put("/api/system-prompts/config")
async def system_prompts_config_set(payload: dict, _: Principal = Depends(get_principal)):
    toggles_in = payload.get("toggles") or {}
    if not isinstance(toggles_in, dict):
        raise HTTPException(status_code=400, detail="toggles deve ser objeto")
    current = _read_system_prompt_config()
    for key, val in toggles_in.items():
        if key not in SYSTEM_PROMPT_TOGGLE_DEFAULTS:
            raise HTTPException(status_code=400, detail=f"toggle desconhecido: {key!r}")
        if not isinstance(val, bool):
            raise HTTPException(status_code=400, detail=f"{key}: deve ser boolean")
        current[key] = val
    _write_system_prompt_config(current)
    return {"ok": True, "toggles": current}


@app.get("/api/system-prompts/sections/{key:path}")
async def system_prompts_section_get(key: str, _: Principal = Depends(get_principal)):
    path, content = _read_section(key)
    return {
        "key": key,
        "source_path": str(path).replace("/workspace/", ""),
        "content": content,
        "present": path.exists(),
    }


@app.put("/api/system-prompts/sections/{key:path}")
async def system_prompts_section_set(
    key: str, payload: dict, _: Principal = Depends(get_principal)
):
    size = _write_section(key, payload.get("content", ""))
    return {"ok": True, "size": size}


def _preview_invocation_block(mode: str | None, parent: str | None) -> str:
    """Reproduz claude_runner._invocation_context_block. Em runtime o bloco
    soh aparece em filha (raiz nao precisa de bloco — '## Equipe' ja sinaliza
    peers chamaveis). No preview com `mode` ausente, mostramos uma dica."""
    if mode is None:
        return (
            "\n\n## Modo de invocacao\n\n"
            "_(Preview: passe `?mode=child&parent=<agente>` pra simular o bloco "
            "que o framework injeta em conv filha. Em raiz, o bloco eh omitido; "
            "o '## Equipe' abaixo lista os peers chamaveis.)_\n"
        )
    if mode == "root":
        # Raiz: framework nao injeta bloco. Preview mostra hint.
        return (
            "\n\n_(Preview: em raiz o framework nao injeta '## Modo de "
            "invocacao'. O bloco '## Equipe' abaixo lista os peers chamaveis.)_\n"
        )
    if mode == "child":
        parent_label = parent or "agente desconhecido"
        return (
            "\n\n## Modo de invocacao\n\n"
            f"Esta conv eh **filha** de **`{parent_label}`** (`ask_agent` ou "
            "handoff de fase). `ask_human`/`ask_agent`/`ask_agents_many` "
            "estao bloqueados aqui (MCP gate 409, hierarquia raiz->filha 1 nivel).\n\n"
            f"Pra solicitar input externo (decisao humana, especialista, "
            f"parecer fora do escopo): **descreva o pedido no fim da resposta "
            f"em formato pronto pra `{parent_label}` encaminhar literal** e "
            "encerre o turno. O pai recebe o reply automatico e roteia.\n\n"
            "Toda instrucao do tipo \"chame `ask_human` X\" / \"chame "
            "`ask_agent <Y>` X\" deve ser lida como \"descreva no reply: "
            "precisa de X\".\n"
        )
    return ""


async def _preview_task_state_block(slug: str | None) -> str:
    if slug is None:
        return (
            "\n\n## Estado da task\n\n"
            "_(Preview: passe `?task_slug=<slug>` na URL pra simular este bloco. "
            "Em runtime, framework injeta quando `topic = task-<slug>`.)_\n"
        )
    task = await db.fetch_one(
        """SELECT id, slug, title, workflow, status, current_step,
                  current_agent, complexity, impact, difficulty,
                  origin_stream, origin_topic, blocked_reason, metadata_extra
             FROM tasks.tasks
            WHERE slug = $1 AND archived_at IS NULL""",
        slug,
    )
    if task is None:
        return (
            "\n\n## Estado da task\n\n"
            f"_(Preview: task `{slug}` nao encontrada no banco.)_\n"
        )
    phases = await db.fetch_all(
        """SELECT step, agent, artifact, completed_at FROM tasks.phases
            WHERE task_id = $1 AND completed_at IS NOT NULL ORDER BY completed_at""",
        task["id"],
    )
    worktrees = await db.fetch_all(
        """SELECT repo, path, branch FROM tasks.worktrees
            WHERE task_id = $1 ORDER BY repo""",
        task["id"],
    )
    meta = task["metadata_extra"]
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    elif not isinstance(meta, dict):
        meta = {}
    baseline = (meta or {}).get("baseline") or {}
    lines: list[str] = ["\n\n## Estado da task\n"]
    lines.append(f"- **Slug:** `{task['slug']}`")
    lines.append(f"- **Titulo:** {task['title']}")
    if task["workflow"]:
        lines.append(f"- **Workflow:** `{task['workflow']}`")
    lines.append(f"- **Status:** `{task['status']}`")
    if task["current_step"]:
        agent_str = f" (agente: `{task['current_agent']}`)" if task["current_agent"] else ""
        lines.append(f"- **Step atual:** `{task['current_step']}`{agent_str}")
    if task["complexity"]:
        lines.append(f"- **Complexidade:** `{task['complexity']}`")
    for k, label in (("impact", "Impacto"), ("difficulty", "Dificuldade")):
        if task[k]:
            lines.append(f"- **{label}:** `{task[k]}`")
    if task["blocked_reason"]:
        lines.append(f"- **Bloqueio:** {task['blocked_reason']}")
    if phases:
        done_str = " -> ".join(
            f"`{p['step']}`" + (f" ({p['artifact']})" if p["artifact"] else "")
            for p in phases
        )
        lines.append(f"- **Fases concluidas:** {done_str}")
    else:
        lines.append("- **Fases concluidas:** _(nenhuma — task acabou de comecar)_")
    if baseline:
        base_str = ", ".join(f"`{repo}@{sha[:8]}`" for repo, sha in sorted(baseline.items()))
        lines.append(f"- **Baselines registradas:** {base_str}")
    else:
        lines.append("- **Baselines registradas:** _(nenhuma)_")
    if worktrees:
        wt_lines = [
            f"  - `{w['repo']}` em `{w['path']}` (branch `{w['branch']}`)"
            for w in worktrees
        ]
        lines.append("- **Worktrees ativas:**")
        lines.extend(wt_lines)
    else:
        lines.append("- **Worktrees ativas:** _(nenhuma)_")
    if task["origin_stream"] and task["origin_topic"]:
        lines.append(
            f"- **Origem:** stream `{task['origin_stream']}`, topico "
            f"`{task['origin_topic']}` (notificacao terminal volta pra ca)"
        )
    lines.append("")
    lines.append(
        "_Estado lido do banco no momento do spawn (snapshot). Pra dado "
        "fresco apos transicao no meio do turn, chame `get_task_state` via MCP._"
    )
    return "\n".join(lines)


def _format_step_overrides_footer(overrides: dict | None) -> str:
    """Rodape de debug visual: mostra quais campos o step esta sobrescrevendo
    no claude_runner. Vazio se overrides ausente/vazio."""
    if not isinstance(overrides, dict) or not overrides:
        return ""
    bits: list[str] = []
    if "model" in overrides:
        bits.append(f"model=`{overrides['model']}`")
    if "effort" in overrides:
        bits.append(f"effort=`{overrides['effort']}`")
    mem = overrides.get("memory") or {}
    if isinstance(mem, dict):
        mem_bits: list[str] = []
        if "enabled" in mem:
            mem_bits.append(f"enabled={mem['enabled']}")
        if "auto_inject_limit" in mem:
            mem_bits.append(f"auto_inject_limit={mem['auto_inject_limit']}")
        if mem_bits:
            bits.append("memory={" + ", ".join(mem_bits) + "}")
    if not bits:
        return ""
    return "\n_Overrides ativos: " + ", ".join(bits) + "._\n"


async def _preview_step_instructions_block(slug: str | None) -> str:
    if slug is None:
        return ""
    row = await db.fetch_one(
        """SELECT workflow, current_step FROM tasks.tasks
            WHERE slug = $1 AND archived_at IS NULL""",
        slug,
    )
    if row is None:
        return ""
    wf_name = row["workflow"]
    step_name = row["current_step"]
    if not wf_name or not step_name:
        return ""
    wf_doc = _load_workflows_full()
    wf = (wf_doc.get("workflows") or {}).get(wf_name) or {}
    step = (wf.get("steps") or {}).get(step_name) or {}
    overrides = step.get("overrides") if isinstance(step, dict) else None
    overrides_footer = _format_step_overrides_footer(overrides)
    instructions = step.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        return (
            "\n\n## Instrucoes da fase atual\n\n"
            f"_(Workflow `{wf_name}` -> step `{step_name}` nao tem campo "
            "`instructions` declarado em `workflows.yaml`.)_\n"
            + overrides_footer
        )
    artifact = step.get("artifact")
    next_steps = step.get("next") or []
    header_lines = [
        "\n\n## Instrucoes da fase atual",
        "",
        f"_Workflow `{wf_name}` -> step `{step_name}`._",
    ]
    if artifact:
        header_lines.append(f"_Artifact esperado: `{artifact}`._")
    if next_steps:
        nxt = " | ".join(f"`{n}`" for n in next_steps)
        header_lines.append(f"_Transicoes validas: {nxt}._")
    header_lines.append("")
    return "\n".join(header_lines) + instructions.rstrip() + "\n" + overrides_footer


@app.get("/api/system-prompts/preview")
async def system_prompts_preview(
    agent: str | None = None,
    mode: str | None = None,
    parent: str | None = None,
    task_slug: str | None = None,
    _: Principal = Depends(get_principal),
):
    """Monta o system prompt completo como sera enviado ao `claude -p` —
    respeita toggles atuais e simula contexto de invocacao via params.

    Params (opcionais, pra simular bloco contextual):
    - `mode`: `root` ou `child`. Sem ele, bloco "## Modo de invocacao" mostra dica.
    - `parent`: nome do agente pai (so faz sentido com `mode=child`).
    - `task_slug`: slug de uma task existente. Quando informado, blocos
      "## Estado da task" e "## Instrucoes da fase atual" sao populados a
      partir do DB + workflows.yaml.

    Reproduz a logica de claude_runner._build_system_prompt — mantenha
    sincronizado se aquela mudar."""
    toggles = _read_system_prompt_config()
    parts: list[str] = []

    if toggles["include_platform_prompt"]:
        try:
            parts.append(PLATFORM_PROMPT_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            parts.append(
                "_(platform.md ausente — claude_runner lancaria RuntimeError "
                "pedindo `make reconcile`. Desligue o toggle ou crie o arquivo.)_"
            )

    if toggles.get("include_invocation_context", True):
        parts.append(_preview_invocation_block(mode, parent))

    if toggles.get("include_task_state", True):
        block = await _preview_task_state_block(task_slug)
        if block:
            parts.append(block)
    if toggles.get("include_step_instructions", True):
        block = await _preview_step_instructions_block(task_slug)
        if block:
            parts.append(block)

    if toggles["include_agent_claude_md"] and agent:
        path = _agent_claude_md_path(agent)
        try:
            txt = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            txt = ""
        if txt.strip():
            parts.append("\n\n# Instrucoes do agente\n\n" + txt)

    if toggles["include_company_context"]:
        try:
            ctx = COMPANY_CONTEXT_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            ctx = ""
        if ctx.strip():
            parts.append("\n\n# Contexto da empresa\n\n" + ctx)

    if toggles["include_company_philosophy"]:
        try:
            phi = COMPANY_PHILOSOPHY_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            phi = ""
        if phi.strip() and "_(opcional" not in phi:
            parts.append("\n\n# Filosofia operacional\n\n" + phi)

    if toggles["include_team_block"] and agent and mode != "child":
        # Reproduz _team_block do runner — mesma SQL, mesma whitelist por
        # agent_policies. Em filha, runner omite (filha nao pode chamar
        # ninguem; listar peers seria desinformacao). Se a logica do
        # runner mudar, sincronize aqui.
        policy = await db.fetch_one(
            "SELECT can_ask FROM messaging.agent_policies WHERE agent = $1",
            agent,
        )
        rows = await db.fetch_all(
            """SELECT u.agent_name AS name, u.full_name AS display_name
                 FROM messaging.users u
                WHERE u.kind = 'bot'
                  AND u.agent_name IS NOT NULL
                  AND u.agent_name <> $1
                  AND u.is_active = true
                ORDER BY u.agent_name""",
            agent,
        )
        whitelist = (policy["can_ask"] if policy and policy["can_ask"] is not None else None)
        peers = []
        for r in rows:
            name = r["name"]
            if whitelist is not None and name not in whitelist:
                continue
            peers.append(f"- **{name}** ({r['display_name']})")
        if peers:
            parts.append(
                "\n\n## Equipe (agentes que voce pode chamar via `ask_agent` ou `ask_agents_many`)\n\n"
                + "\n".join(peers)
            )
        else:
            parts.append(
                "\n\n## Equipe\n\n"
                "_Nenhum outro agente disponivel pra `ask_agent` agora. "
                "Use `ask_human` se precisar delegar._"
            )

    return {"agent": agent, "toggles": toggles, "content": "".join(parts)}


# ---------- Cost budgets ----------

@app.get("/api/cost-budgets")
async def cost_budgets_list(_: Principal = Depends(get_principal)):
    """Lista budgets configurados + gasto de hoje (UTC) por agente."""
    rows = await db.fetch_all(
        """
        WITH today_spend AS (
            SELECT agent, COALESCE(SUM(cost_usd), 0)::float AS spent
              FROM telemetry.events
             WHERE (ts AT TIME ZONE 'UTC')::date = (now() AT TIME ZONE 'UTC')::date
             GROUP BY agent
        )
        SELECT b.agent,
               b.daily_usd_limit::float AS daily_usd_limit,
               b.alert_message,
               COALESCE(t.spent, 0) AS spent_today_usd
          FROM web.cost_budgets b
          LEFT JOIN today_spend t ON t.agent = b.agent
         ORDER BY b.agent
        """,
    )
    return {
        "items": [
            {
                "agent": r["agent"],
                "daily_usd_limit": r["daily_usd_limit"],
                "alert_message": r["alert_message"],
                "spent_today_usd": float(r["spent_today_usd"]),
                "pct": (
                    float(r["spent_today_usd"]) / r["daily_usd_limit"]
                    if r["daily_usd_limit"] else 0
                ),
                "over": float(r["spent_today_usd"]) >= r["daily_usd_limit"],
            } for r in rows
        ],
    }


@app.post("/api/cost-budgets")
async def cost_budget_upsert(payload: dict, _: Principal = Depends(get_principal)):
    agent = (payload.get("agent") or "").strip()
    limit = payload.get("daily_usd_limit")
    if not agent or not limit:
        raise HTTPException(status_code=400, detail="agent + daily_usd_limit obrigatorios")
    try:
        limit_f = float(limit)
        if limit_f <= 0:
            raise ValueError("limit must be positive")
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="daily_usd_limit invalido")
    await db.execute(
        """INSERT INTO web.cost_budgets (agent, daily_usd_limit, alert_message)
           VALUES ($1, $2, $3)
           ON CONFLICT (agent) DO UPDATE SET
             daily_usd_limit = EXCLUDED.daily_usd_limit,
             alert_message = EXCLUDED.alert_message,
             updated_at = now()""",
        agent, limit_f, payload.get("alert_message"),
    )
    return {"ok": True, "agent": agent, "daily_usd_limit": limit_f}


@app.delete("/api/cost-budgets/{agent}")
async def cost_budget_delete(agent: str, _: Principal = Depends(get_principal)):
    await db.execute("DELETE FROM web.cost_budgets WHERE agent = $1", agent)
    return {"ok": True}


# ---------- Memory ----------

@app.get("/api/memory/agents")
async def memory_list_agents():
    rows = await db.fetch_all(
        "SELECT agent, COUNT(*) AS count FROM memory.facts GROUP BY agent ORDER BY agent",
    )
    return {"items": [dict(r) for r in rows]}


@app.get("/api/memory/{agent}")
async def memory_list(agent: str, q: str = "", tag: str | None = None, limit: int = 50):
    params: list = [agent]
    where = ["agent = $1"]
    if q:
        where.append(f"to_tsvector('simple', key || ' ' || value) @@ plainto_tsquery('simple', ${len(params)+1})")
        params.append(q)
    if tag:
        where.append(f"${len(params)+1} = ANY(tags)")
        params.append(tag)
    sql = f"""SELECT id, key, value, tags, created_at, updated_at
                FROM memory.facts
               WHERE {' AND '.join(where)}
               ORDER BY updated_at DESC
               LIMIT {min(int(limit), 500)}"""
    rows = await db.fetch_all(sql, *params)
    items = [
        {
            "id": r["id"], "key": r["key"], "value": r["value"], "tags": r["tags"],
            "created_at": int(r["created_at"].timestamp()),
            "updated_at": int(r["updated_at"].timestamp()),
        } for r in rows
    ]
    total = await db.fetch_one("SELECT COUNT(*) AS c FROM memory.facts WHERE agent = $1", agent)
    return {"items": items, "count": total["c"]}


@app.post("/api/memory/{agent}")
async def memory_save(agent: str, payload: dict):
    key = payload.get("key")
    value = payload.get("value")
    tags = payload.get("tags") or []
    if not key or value is None:
        raise HTTPException(status_code=400, detail="key e value obrigatorios")
    await db.execute(
        """INSERT INTO memory.facts (agent, key, value, tags)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (agent, key) DO UPDATE SET
             value = EXCLUDED.value, tags = EXCLUDED.tags, updated_at = now()""",
        agent, key, value, list(tags),
    )
    return {"ok": True}


@app.put("/api/memory/{agent}/{key:path}")
async def memory_edit(agent: str, key: str, payload: dict):
    value = payload.get("value")
    tags = payload.get("tags")
    if value is None and tags is None:
        raise HTTPException(status_code=400, detail="informe 'value' e/ou 'tags'")
    sets: list[str] = []
    params: list = [agent, key]
    if value is not None:
        params.append(value)
        sets.append(f"value = ${len(params)}")
    if tags is not None:
        params.append(list(tags))
        sets.append(f"tags = ${len(params)}")
    sets.append("updated_at = now()")
    sql = (
        f"UPDATE memory.facts SET {', '.join(sets)} "
        "WHERE agent = $1 AND key = $2 "
        "RETURNING id, key, value, tags, "
        "EXTRACT(EPOCH FROM created_at)::int AS created_at, "
        "EXTRACT(EPOCH FROM updated_at)::int AS updated_at"
    )
    row = await db.fetch_one(sql, *params)
    if row is None:
        raise HTTPException(status_code=404, detail=f"fact '{key}' nao existe em {agent}")
    return {"ok": True, "item": dict(row)}


@app.delete("/api/memory/{agent}/{key:path}")
async def memory_delete(agent: str, key: str):
    status = await db.execute(
        "DELETE FROM memory.facts WHERE agent = $1 AND key = $2", agent, key,
    )
    if isinstance(status, str) and not status.endswith(" 1"):
        raise HTTPException(status_code=404, detail=f"fact '{key}' nao existe em {agent}")
    return {"ok": True}


# ---------- Onboarding wizard ----------

ONBOARDED_FLAG = Path("/workspace/company/.onboarded")


@app.get("/api/onboard/status")
async def onboard_status(_: Principal = Depends(get_principal)):
    """Detecta se o sistema precisa de onboarding inicial. Fresh =
    flag /workspace/.onboarded NAO existe. Tambem retorna count atual
    de agentes pra UI exibir."""
    rows = await db.fetch_all(
        "SELECT COUNT(*) AS c FROM messaging.users WHERE kind='bot'"
    )
    agents_count = int(rows[0]["c"]) if rows else 0
    return {
        "fresh": not ONBOARDED_FLAG.exists(),
        "agents_count": agents_count,
        "has_context": COMPANY_CONTEXT_PATH.exists(),
    }


@app.post("/api/onboard/propose-agents")
async def onboard_propose_agents(payload: dict, _: Principal = Depends(get_principal)):
    """Chama LLM (via container executor) pra propor agentes alinhados
    com empresa+filosofia. Body: {company_md, philosophy_md}.
    Retorna {agents: [{slug, display_name, role, why, draft_claude_md}]}."""
    company_md = (payload.get("company_md") or "").strip()
    philosophy_md = (payload.get("philosophy_md") or "").strip()
    if not company_md:
        raise HTTPException(status_code=400, detail="company_md obrigatorio")

    container_name = os.environ.get(
        "HIRE_AGENT_CONTAINER", "agent-framework-agent-executor-1"
    )
    prompt = (
        "Voce eh um arquiteto de empresa virtual baseada em agentes Claude Code.\n\n"
        "EMPRESA (CONTEXT.md):\n"
        f"```markdown\n{company_md}\n```\n\n"
        "FILOSOFIA OPERACIONAL ATIVA:\n"
        f"```markdown\n{philosophy_md or '(nenhuma)'}\n```\n\n"
        "TAREFA: proponha 3-7 agentes que essa empresa deveria ter, "
        "alinhados com a filosofia.\n\n"
        "Pra cada agente:\n"
        "- slug: lowercase, sem espacos, [a-z0-9-], comeca com letra, max 30 chars\n"
        "- display_name: capitalizado em pt-BR\n"
        "- role: 1 frase do que faz\n"
        "- why: 1 frase justificando essa empresa precisar dele\n"
        "- draft_claude_md: CLAUDE.md pronto pra usar (~200-400 palavras), "
        "incluindo papel, responsabilidades, NAO-responsabilidades, estilo, "
        "alinhado com a filosofia.\n\n"
        "OUTPUT estrito JSON valido (nada antes ou depois):\n"
        "{\"agents\":[{\"slug\":\"...\",\"display_name\":\"...\",\"role\":\"...\","
        "\"why\":\"...\",\"draft_claude_md\":\"...\"}]}"
    )

    # Mock pra E2E (CLAUDE_MOCK no executor) ou se container morto
    if os.environ.get("CLAUDE_MOCK"):
        return {
            "agents": [
                {
                    "slug": "po", "display_name": "Product Owner",
                    "role": "Define prioridades e mantém backlog", "why": "Toda empresa precisa de direcao",
                    "draft_claude_md": "# PO\n\n_(mock)_\n",
                },
                {
                    "slug": "dev", "display_name": "Developer",
                    "role": "Implementa features", "why": "Quem escreve codigo",
                    "draft_claude_md": "# Dev\n\n_(mock)_\n",
                },
            ],
        }

    client = docker.from_env()
    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        raise HTTPException(
            status_code=503,
            detail=f"container {container_name} nao encontrado pra rodar Claude. "
                   "Suba os agentes ou ajuste HIRE_AGENT_CONTAINER.",
        )

    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    log.info("onboard.proposing", container=container_name, prompt_len=len(prompt))
    import asyncio as _a
    def _run():
        return container.exec_run(cmd, stdout=True, stderr=True, demux=False, user="node",
                                  environment={"HOME": "/home/node"})
    rc, out = await _a.get_event_loop().run_in_executor(None, _run)
    if rc != 0:
        raise HTTPException(status_code=502, detail=f"claude rc={rc}: {out[:300] if out else ''}")
    try:
        wrapper = json.loads(out)
        result_text = wrapper.get("result", "")
        # Extrai bloco JSON do result_text (pode vir com ```json ... ```)
        import re as _re
        m = _re.search(r"\{[\s\S]*\"agents\"[\s\S]*\}", result_text)
        if not m:
            raise ValueError("LLM nao retornou JSON com 'agents'")
        proposal = json.loads(m.group(0))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"parse falhou: {e}; raw: {(out or b'')[:300]}")
    return proposal


@app.post("/api/onboard/apply")
async def onboard_apply(payload: dict, _: Principal = Depends(get_principal)):
    """Aplica wizard: salva CONTEXT.md + philosophy.md + cria N agentes
    em batch via hire.apply (skip draft, usa drafts ja gerados).
    Body: {company_md, philosophy_md, agents: [{slug, display_name, role, draft_claude_md}]}.
    Marca /workspace/.onboarded."""
    from . import hire as _hire
    company_md = payload.get("company_md", "")
    philosophy_md = payload.get("philosophy_md", "")
    agents = payload.get("agents") or []
    if not isinstance(agents, list) or not agents:
        raise HTTPException(status_code=400, detail="agents obrigatorio (lista nao-vazia)")

    # Salva docs primeiro
    if company_md:
        _write_company_file(COMPANY_CONTEXT_PATH, company_md)
    if philosophy_md:
        _write_company_file(COMPANY_PHILOSOPHY_PATH, philosophy_md)

    # Cria cada agente via hire.apply com draft pronto. apply gera o yaml_entry
    # do agent.yaml.example padrao + o CLAUDE.md fornecido.
    created: list[str] = []
    errors: list[dict] = []
    for a in agents:
        slug = (a.get("slug") or "").strip()
        display = (a.get("display_name") or slug).strip()
        claude_md = a.get("draft_claude_md") or f"# {display}\n\n{a.get('role','')}\n"
        # yaml_entry minimo (memory + ask_human + ask_agent permitidos)
        yaml_entry = (
            f"  - name: {slug}\n"
            f"    display_name: \"{display}\"\n"
            f"    description: \"{(a.get('role') or '').replace(chr(34), '')}\"\n"
            f"    streams: [{slug}]\n"
            f"    pool_size: 2\n"
            f"    idle_timeout_sec: 900\n"
            f"    write_access: [company]\n"
            f"    read_access: []\n"
            f"    memory: true\n"
            f"    allowed_tools:\n"
            f"      - Read\n      - Write\n      - Edit\n"
            f"      - mcp__agent_framework__ask_human\n"
            f"      - mcp__agent_framework__ask_agent\n"
            f"      - mcp__agent_framework__memory_save\n"
            f"      - mcp__agent_framework__memory_recall\n"
            f"      - mcp__agent_framework__memory_list\n"
        )
        try:
            await _hire.apply({
                "name": slug,
                "yaml_entry": yaml_entry,
                "claude_md": claude_md,
            })
            created.append(slug)
        except Exception as e:
            log.exception("onboard.agent_apply_failed", slug=slug)
            errors.append({"slug": slug, "error": str(e)[:300]})

    # Marca onboarded mesmo com erros parciais (idempotent)
    try:
        ONBOARDED_FLAG.parent.mkdir(parents=True, exist_ok=True)
        ONBOARDED_FLAG.write_text(
            json.dumps({"created": created, "errors": errors, "at": time.time()}),
            encoding="utf-8",
        )
    except Exception:
        log.exception("onboard.flag_write_failed")

    return {"ok": True, "created": created, "errors": errors}


# ---------- Hire ----------

@app.post("/api/hire/draft")
async def hire_draft(payload: dict):
    from . import hire
    return await hire.draft(payload)


@app.post("/api/hire/apply")
async def hire_apply(payload: dict):
    from . import hire
    return await hire.apply(payload)


# ---------- SPA fallback ----------
# DEPOIS de todas as rotas API e mounts: rotas nao reservadas voltam
# index.html pra deep links do client-side router (?ask=<id>, futuras
# rotas /conv/<id>, etc). FastAPI processa rotas em ordem de registro,
# entao precisa ser a ULTIMA.

_RESERVED_PREFIXES = ("api/", "static/", "_app/", "health", "sw.js", "manifest.webmanifest", "docs", "openapi", "redoc")


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str):
    if full_path.startswith(_RESERVED_PREFIXES):
        raise HTTPException(status_code=404)
    f = _index_file()
    if not f.exists():
        raise HTTPException(status_code=503, detail="frontend nao instalado")
    # D-96: mesma estrategia do `/` — index.html nao deve cachear
    # (referencia bundles hashed; se cachear, app trava em versao velha
    # mesmo apos rebuild). Bundles `/_app/*` sao immutable via middleware.
    return FileResponse(f, media_type="text/html", headers=_NO_CACHE)
