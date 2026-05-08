"""Broker HTTP — API de messaging (messages/events/subscriptions/users/asks/conversations).

Rotas (prefixadas em /api):
  POST   /messages                   posta mensagem num (stream, topic)
  GET    /messages                   lista mensagens (filtros: stream, topic, since_id)
  GET    /events                     SSE stream de mensagens novas
  POST   /streams                    cria stream (admin)
  GET    /streams                    lista streams
  DELETE /streams/{name}             remove stream (admin; 409 se tem conv)
  POST   /subscriptions              subscreve user/bot em stream (admin)
  GET    /subscriptions              lista subs do principal atual
  POST   /users                      cria user/bot (admin)
  GET    /users                      lista users
  GET    /users/me                   retorna principal atual
  DELETE /users/{username}           remove user (admin; 409 se tem mensagem)
  POST   /asks                       registra pending ask (usado por agente)
  GET    /asks                       lista pending asks (do principal/admin)
  GET    /conversations              lista conversas com metadata + last message

Autenticacao: Bearer token via header Authorization. Ver auth.py.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
from datetime import datetime, timezone
from typing import Any, AsyncIterator

# D-84: limiar de "stuck" — turno aberto sem run_end ha mais que isso e
# considerado travado. Ler do mesmo env de main.py (par; documentado la);
# se divergir no futuro, mover pra modulo compartilhado.
RUNNER_STUCK_SEC = int(os.environ.get("RUNNER_STUCK_SEC", "600"))

import aiohttp
import asyncpg
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import db
from .auth import Principal, get_principal, require_admin


log = structlog.get_logger("broker")
router = APIRouter(prefix="/api")


# ---------- Models ----------

class MessageIn(BaseModel):
    stream: str
    topic: str
    content: str
    client_id: str | None = None
    # Migration 026: 'regular' (default) dispara turn no listener; 'echo'
    # eh forward visual de reply de conv-filha (D-100), pula dispatch mas
    # continua visivel pro humano via SSE. Validado por CHECK no banco.
    kind: str = "regular"
    # D-87: quando msg cria conv nova (primeira em (stream, topic)), persiste
    # parent_conv_id na coluna homonima. Ignorado se conv ja existe (invariante:
    # parent nao muda depois de criada). Agente passa no ask_agent; reactor
    # passa em complete_phase. Hierarquia vira lookup O(1) via FK, sem heuristica.
    parent_conv_id: int | None = None
    # Override de sender. So tem efeito quando o caller eh service token
    # (principal sem user_id — scheduler/reactor). Tokens de humano ignoram
    # (impede impersonation). Username precisa existir em messaging.users.
    # Default (None) mantem comportamento legado: service token cai em
    # `system-bot`.
    as_username: str | None = None


class MessageOut(BaseModel):
    id: int
    conversation_id: int
    stream: str
    topic: str
    sender_id: int
    sender_username: str
    content: str
    sent_at: str


class StreamIn(BaseModel):
    name: str
    description: str | None = None


class UserIn(BaseModel):
    email: str
    username: str
    full_name: str
    kind: str                       # 'human' | 'bot'
    agent_name: str | None = None
    is_admin: bool = False


class SubscriptionIn(BaseModel):
    user_id: int
    stream: str


class CursorIn(BaseModel):
    stream: str
    last_read_message_id: int


class AskIn(BaseModel):
    stream: str
    topic: str
    question: str
    context: str | None = None
    blocking: bool = True
    # D-111: kind distingue ask_human (push humano + badge Mine) vs
    # ask_agent (agente target responde, silencioso pra humano).
    kind: str = "ask_human"
    target_agent: str | None = None


# ---------- Helpers ----------

_TOMBSTONE_TTL_SEC = int(os.environ.get("TOMBSTONE_TTL_SEC", "300"))


async def _get_or_create_conversation(
    conn: asyncpg.Connection,
    stream: str,
    topic: str,
    parent_conv_id: int | None = None,
) -> tuple[int, int]:
    """Retorna (conversation_id, stream_id). Cria se nao existe.

    Bloqueia re-criacao se `(stream, topic)` esta na tombstone
    `messaging.deleted_topics` ha menos de TOMBSTONE_TTL_SEC (default 5min).
    Motivo: quando o humano DELETA uma conv via PWA, o backend dispara
    cancel nos runners ativos; o dispatcher posta msg de confirmacao do
    cancel que passaria por aqui e re-criaria a conv. O tombstone corta
    esse loop — dispatcher recebe 410 Gone, loga e descarta.

    D-87: `parent_conv_id` persiste na coluna homonima quando a conv eh
    criada agora (INSERT succeeded). Ignorado se conv ja existia (ON CONFLICT
    DO UPDATE caminho) — invariante: parent nao muda apos criacao. Ciclo
    e FK invalido sao validados antes; falha silenciosa (log warning) em
    vez de exceção pra nao quebrar fluxo de msg que ja foi aceita.
    """
    ts_row = await conn.fetchrow(
        """SELECT dt.deleted_at
             FROM messaging.deleted_topics dt
             JOIN messaging.streams s ON s.id = dt.stream_id
            WHERE s.name = $1 AND dt.topic_name = $2
              AND dt.deleted_at > now() - ($3 || ' seconds')::interval
            LIMIT 1""",
        stream, topic, str(_TOMBSTONE_TTL_SEC),
    )
    if ts_row is not None:
        raise HTTPException(
            status_code=410,
            detail=f"topic {topic!r} was deleted recently; posts are blocked",
        )
    # Validacao defensiva do parent_conv_id: precisa existir, nao criar ciclo,
    # E (D-96) o pai precisa ele mesmo ser raiz. Profundidade maxima da arvore
    # eh 1: raiz -> filha. Tentativa de criar neta retorna 409 — agente que
    # virou filho via ask_agent nao deve delegar mais; deve responder ao pai
    # que decide. Erro explicito ajuda detectar prompts/codigo errados.
    safe_parent: int | None = None
    if parent_conv_id is not None:
        parent_row = await conn.fetchrow(
            "SELECT id, parent_conv_id FROM messaging.conversations WHERE id = $1",
            parent_conv_id,
        )
        if parent_row is None:
            log.warning("broker.invalid_parent_conv_id", parent_conv_id=parent_conv_id, stream=stream, topic=topic)
        elif parent_row["parent_conv_id"] is not None:
            log.warning(
                "broker.depth_violation",
                parent_conv_id=parent_conv_id,
                grandparent_conv_id=parent_row["parent_conv_id"],
                stream=stream, topic=topic,
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    f"depth violation: conv {parent_conv_id} is already a child "
                    f"(parent={parent_row['parent_conv_id']}); cannot have its "
                    "own child. Max hierarchy is root -> child (D-96). "
                    "A child needing more info should reply to its parent, "
                    "not delegate."
                ),
            )
        else:
            safe_parent = parent_conv_id
    row = await conn.fetchrow(
        """
        WITH s AS (SELECT id FROM messaging.streams WHERE name = $1),
        conv_ins AS (
            INSERT INTO messaging.conversations (stream_id, topic_name, parent_conv_id)
            SELECT id, $2, $3 FROM s
            ON CONFLICT (stream_id, topic_name) DO UPDATE SET last_message_at = now()
            RETURNING id, stream_id, (xmax = 0) AS inserted
        )
        SELECT id, stream_id, inserted FROM conv_ins
        """,
        stream, topic, safe_parent,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"stream {stream!r} does not exist")
    return row["id"], row["stream_id"]


# ---------- Messages ----------

@router.post("/messages", response_model=MessageOut)
async def post_message(msg: MessageIn, principal: Principal = Depends(get_principal)) -> MessageOut:
    """Envia mensagem. Cria conversation se nao existe.
    Auto-resolve pending_ask se mensagem vem de quem nao eh o asker.
    """
    if principal.user_id is None:
        # Service tokens (reactor/scheduler) postam como system-bot por default,
        # ou como `as_username` se especificado (scheduler usa pra postar em
        # nome de um humano/bot e fazer a conv aparecer pra esse user).
        if msg.as_username:
            row = await db.fetch_one(
                "SELECT id, username FROM messaging.users WHERE username = $1",
                msg.as_username,
            )
            if row is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"as_username {msg.as_username!r} does not exist",
                )
            sender_id = row["id"]
            effective_username = row["username"]
        else:
            row = await db.fetch_one(
                "SELECT id FROM messaging.users WHERE username = 'system-bot' AND kind = 'bot'",
            )
            if row is None:
                raise HTTPException(status_code=500, detail="system-bot not initialized")
            sender_id = row["id"]
            effective_username = "system-bot"
    else:
        sender_id = principal.user_id
        effective_username = principal.username

    async with db.connection() as conn:
        async with conn.transaction():
            conv_id, stream_id = await _get_or_create_conversation(
                conn, msg.stream, msg.topic,
                parent_conv_id=msg.parent_conv_id,
            )
            # D-93 (generaliza D-76/D-77): conv filha completed vira read-only
            # pro humano. Filha = `parent_conv_id IS NOT NULL`. Completed =
            # ultimo run do agente terminou (run_end com subtype ok), sem
            # ask_human aberto, e ultima msg eh do bot da stream e nao eh uma
            # pergunta. Caso classico: ask_agent reply (D-76), task-<slug>
            # filha pos-complete_phase, notify_human terminal.
            #
            # Quem ainda pode postar: o proprio `<stream>-bot` (output
            # legitimo pos-reply, raro mas existe) e qualquer service token
            # (reactor postando handoff de volta na conv). Bloqueio mira
            # humano que poderia responder no lugar errado.
            conv_meta = await conn.fetchrow(
                """SELECT c.parent_conv_id,
                          (SELECT u.username FROM messaging.messages m
                             JOIN messaging.users u ON u.id = m.sender_id
                            WHERE m.conversation_id = c.id
                            ORDER BY m.id DESC LIMIT 1) AS last_sender,
                          (SELECT m.content FROM messaging.messages m
                            WHERE m.conversation_id = c.id
                            ORDER BY m.id DESC LIMIT 1) AS last_content,
                          (SELECT le.kind FROM telemetry.live_events le
                            WHERE le.conversation_id = c.id
                              AND le.kind IN ('run_start', 'run_end')
                            ORDER BY le.id DESC LIMIT 1) AS last_run_kind,
                          (SELECT le.data->>'subtype' FROM telemetry.live_events le
                            WHERE le.conversation_id = c.id
                              AND le.kind IN ('run_start', 'run_end')
                            ORDER BY le.id DESC LIMIT 1) AS last_run_subtype,
                          EXISTS(SELECT 1 FROM messaging.pending_asks pa
                                  WHERE pa.conversation_id = c.id
                                    AND pa.resolved_at IS NULL
                                    AND pa.kind = 'ask_human') AS has_open_ask
                     FROM messaging.conversations c
                    WHERE c.id = $1""",
                conv_id,
            )
            # D-96: qualquer conv filha eh read-only pro humano. Antes (D-93)
            # so filha completed era; agora generaliza — humano so responde
            # na raiz, agente da raiz decide se delega/escala. Bots (service
            # tokens, agentes via broker token) continuam podendo postar —
            # bloqueio mira o humano que escolheria conv errada.
            if (
                conv_meta
                and conv_meta["parent_conv_id"] is not None
                and principal.kind == "human"
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "This is a delegated sub-conversation (read-only). "
                        "Reply on the parent conversation; the parent agent "
                        "will pass it through."
                    ),
                )
            try:
                m_row = await conn.fetchrow(
                    """
                    INSERT INTO messaging.messages (conversation_id, sender_id, content, client_id, kind)
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING id, sent_at
                    """,
                    conv_id, sender_id, msg.content, msg.client_id, msg.kind,
                )
            except asyncpg.UniqueViolationError:
                # idempotency: client_id ja existe -> devolve a msg existente
                m_row = await conn.fetchrow(
                    """
                    SELECT id, sent_at FROM messaging.messages
                    WHERE conversation_id = $1 AND client_id = $2
                    """,
                    conv_id, msg.client_id,
                )
            # auto-resolve pending_ask (se existe e sender != asker).
            # Migration 026: kind='echo' eh forward visual, nao eh resposta —
            # pular auto-resolve pra nao fechar ask_human aberto na conv pai
            # com base num eco que veio de outra conv.
            if msg.kind == "regular":
                await conn.execute(
                    """
                    UPDATE messaging.pending_asks
                       SET resolved_at = now(), answer_message_id = $2
                     WHERE conversation_id = $1
                       AND asker_id <> $3
                       AND resolved_at IS NULL
                    """,
                    conv_id, m_row["id"], sender_id,
                )
    log.info("broker.message_posted", conv_id=conv_id, sender=effective_username, stream=msg.stream, topic=msg.topic)
    return MessageOut(
        id=m_row["id"],
        conversation_id=conv_id,
        stream=msg.stream,
        topic=msg.topic,
        sender_id=sender_id,
        sender_username=effective_username,
        content=msg.content,
        sent_at=m_row["sent_at"].isoformat(),
    )


@router.get("/messages")
async def list_messages(
    stream: str | None = None,
    topic: str | None = None,
    conversation_id: int | None = None,
    since_id: int = 0,
    limit: int = Query(default=100, le=500),
    principal: Principal = Depends(get_principal),
):
    """Lista mensagens, mais recentes por ultimo. Filtros acumulativos."""
    where = ["m.id > $1"]
    params: list[Any] = [since_id]
    if conversation_id is not None:
        where.append(f"m.conversation_id = ${len(params)+1}")
        params.append(conversation_id)
    if stream is not None:
        where.append(f"s.name = ${len(params)+1}")
        params.append(stream)
    if topic is not None:
        where.append(f"c.topic_name = ${len(params)+1}")
        params.append(topic)
    sql = f"""
        SELECT m.id, m.conversation_id, s.name AS stream, c.topic_name AS topic,
               m.sender_id, u.username AS sender_username, m.content, m.sent_at,
               m.kind
          FROM messaging.messages m
          JOIN messaging.conversations c ON c.id = m.conversation_id
          JOIN messaging.streams s ON s.id = c.stream_id
          JOIN messaging.users u ON u.id = m.sender_id
         WHERE {' AND '.join(where)}
         ORDER BY m.id ASC
         LIMIT {int(limit)}
    """
    rows = await db.fetch_all(sql, *params)
    return [
        dict(
            id=r["id"], conversation_id=r["conversation_id"], stream=r["stream"],
            topic=r["topic"], sender_id=r["sender_id"], sender_username=r["sender_username"],
            content=r["content"], sent_at=r["sent_at"].isoformat(),
            kind=r["kind"],
        ) for r in rows
    ]


# ---------- Events SSE ----------

@router.get("/events")
async def events_sse(
    request: Request,
    streams: str | None = Query(default=None, description="comma-separated stream names; omit = all"),
    principal: Principal = Depends(get_principal),
):
    """SSE stream: emite cada mensagem nova como `data: {...}\\n\\n`.
    Clients devem reconectar em caso de queda (browser EventSource faz auto).
    """
    filter_names = [s.strip() for s in streams.split(",")] if streams else None

    async def event_gen() -> AsyncIterator[str]:
        import os
        # Conexao dedicada fora do pool pro LISTEN (LISTEN mantem conn idle).
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        # Tuple (channel, payload) pra discriminar origem.
        queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

        def _cb(_conn, _pid, channel, payload):
            queue.put_nowait((channel, payload))
        try:
            # msg_all: evento principal (nova msg) — enrich via SELECT.
            # ask_new: pending_ask INSERT — corrige race D-77 (msg commita
            # antes do pending_ask; sem esse listen o frontend re-fetcha
            # /api/conversations antes do has_pending_ask virar true).
            # conv_activity: run_start/run_end em telemetry — corrige stale
            # is_queued D-81 (pool libera, runner spawna, mas sidebar
            # continuava mostrando QUEUED ate proxima msg).
            await conn.add_listener("msg_all", _cb)
            await conn.add_listener("ask_new", _cb)
            await conn.add_listener("conv_activity", _cb)
            # Heartbeat para manter a conexao viva
            while True:
                if await request.is_disconnected():
                    break
                try:
                    channel, payload = await asyncio.wait_for(queue.get(), timeout=15)
                    data = json.loads(payload)
                    if channel == "ask_new":
                        # Slim; frontend so precisa do trigger pra re-fetch.
                        # Nao passa por filter_names (ask e global).
                        yield f"data: {json.dumps(data)}\n\n"
                        continue
                    if channel == "conv_activity":
                        # Frontend so precisa saber que mudou algo na conv
                        # (run_start/run_end) pra re-fetchar /api/conversations.
                        # payload ja tem conversation_id + kind + ts.
                        data["_channel"] = "conv_activity"
                        yield f"data: {json.dumps(data)}\n\n"
                        continue
                    # channel == "msg_all": payload do pg_notify eh slim
                    # (id + conversation_id + sender_id) — limite de 8000
                    # bytes impede inlinear content. Busca o resto por id.
                    row = await db.fetch_one(
                        """SELECT s.name AS stream, c.topic_name AS topic,
                                  u.username AS sender_username,
                                  m.content, m.sent_at
                             FROM messaging.messages m
                             JOIN messaging.conversations c ON c.id = m.conversation_id
                             JOIN messaging.streams s ON s.id = c.stream_id
                             JOIN messaging.users u ON u.id = m.sender_id
                            WHERE m.id = $1""",
                        data["id"],
                    )
                    if row is None:
                        continue
                    if filter_names is not None and row["stream"] not in filter_names:
                        continue
                    data["stream"] = row["stream"]
                    data["topic"] = row["topic"]
                    data["sender_username"] = row["sender_username"]
                    data["content"] = row["content"]
                    data["sent_at"] = row["sent_at"].isoformat()
                    yield f"data: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"   # keepalive
        finally:
            await conn.remove_listener("msg_all", _cb)
            await conn.remove_listener("ask_new", _cb)
            await conn.remove_listener("conv_activity", _cb)
            await conn.close()

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# ---------- Streams ----------

@router.post("/streams")
async def create_stream(data: StreamIn, _: Principal = Depends(require_admin)):
    # is_active = true no UPSERT — se stream foi soft-deleted antes
    # (agente saiu do agents.yaml e voltou), reativa ao recriar.
    row = await db.fetch_one(
        """INSERT INTO messaging.streams (name, description, is_active)
           VALUES ($1, $2, true)
           ON CONFLICT (name) DO UPDATE SET
              description = COALESCE(EXCLUDED.description, messaging.streams.description),
              is_active = true
           RETURNING id, name, description, is_active""",
        data.name, data.description,
    )
    return {"id": row["id"], "name": row["name"], "description": row["description"],
            "is_active": row["is_active"]}


@router.get("/streams")
async def list_streams(principal: Principal = Depends(get_principal)):
    from . import app_settings as _s
    # Inclui is_active pra UI poder esconder inativas. Por default lista
    # ativas+inativas (preservar acesso ao historico); UI filtra.
    rows = await db.fetch_all(
        "SELECT id, name, description, is_active FROM messaging.streams ORDER BY name"
    )
    streams = [dict(id=r["id"], name=r["name"], description=r["description"],
                    is_active=r["is_active"]) for r in rows]
    default = await _s.get_default_stream()
    return {"streams": streams, "default": default}


@router.patch("/streams/{name}/active")
async def set_stream_active(
    name: str, payload: dict, _: Principal = Depends(require_admin),
):
    """Soft-delete/reativacao. Body: {"is_active": bool}.

    Soft-delete preserva historico mas remove a stream de listagens da UI
    e do '## Equipe' do system prompt. Usado pelo reconcile quando agente
    sai do agents.yaml — alternativa segura ao hard-delete que falha com
    409 se houver convs.
    """
    if "is_active" not in payload or not isinstance(payload["is_active"], bool):
        raise HTTPException(status_code=400, detail="body requires {'is_active': bool}")
    row = await db.fetch_one(
        "UPDATE messaging.streams SET is_active = $2 WHERE name = $1 "
        "RETURNING id, name, is_active",
        name, payload["is_active"],
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"stream {name!r} does not exist")
    return {"id": row["id"], "name": row["name"], "is_active": row["is_active"]}


@router.delete("/streams/{name}")
async def delete_stream(name: str, _: Principal = Depends(require_admin)):
    """Remove stream do broker. Falha 409 se houver conversations (preservar
    historico). Subscriptions sem conversations caem via CASCADE.

    Usado por reconcile pra prune de streams que sairam do agents.yaml.
    """
    row = await db.fetch_one("SELECT id FROM messaging.streams WHERE name = $1", name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"stream {name!r} does not exist")
    stream_id = row["id"]
    conv_count = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM messaging.conversations WHERE stream_id = $1", stream_id,
    )
    if conv_count["n"] > 0:
        raise HTTPException(
            status_code=409,
            detail=f"stream {name!r} has {conv_count['n']} conversation(s); delete manually via SQL if intentional",
        )
    await db.execute("DELETE FROM messaging.streams WHERE id = $1", stream_id)
    return {"ok": True, "deleted": name}


# ---------- Subscriptions ----------

@router.post("/subscriptions")
async def create_subscription(data: SubscriptionIn, _: Principal = Depends(require_admin)):
    row = await db.fetch_one(
        """
        INSERT INTO messaging.subscriptions (user_id, stream_id)
        SELECT $1, s.id FROM messaging.streams s WHERE s.name = $2
        ON CONFLICT DO NOTHING
        RETURNING user_id, stream_id
        """,
        data.user_id, data.stream,
    )
    if row is None:
        # pode ter falhado por stream inexistente ou ja subscrito
        exists = await db.fetch_one("SELECT 1 FROM messaging.streams WHERE name = $1", data.stream)
        if exists is None:
            raise HTTPException(status_code=404, detail=f"stream {data.stream!r} does not exist")
    return {"ok": True}


@router.get("/subscriptions")
async def list_subscriptions(principal: Principal = Depends(get_principal)):
    if principal.user_id is None:
        return []
    rows = await db.fetch_all(
        """SELECT s.id, s.name, s.description
             FROM messaging.subscriptions sub
             JOIN messaging.streams s ON s.id = sub.stream_id
            WHERE sub.user_id = $1
            ORDER BY s.name""",
        principal.user_id,
    )
    return [dict(id=r["id"], name=r["name"], description=r["description"]) for r in rows]


# ---------- Cursor persistido (catch-up de unread — D-52) ----------
# pg_notify eh fire-and-forget; sem cursor, msgs que chegam enquanto o bot
# esta down viram fantasmas. InternalClient.start() busca cursor antes do
# LISTEN; ao processar msg, avisa o broker pra avancar cursor.

@router.get("/subscriptions/cursor")
async def get_subscription_cursor(stream: str, principal: Principal = Depends(get_principal)):
    if principal.user_id is None:
        raise HTTPException(status_code=401, detail="missing user_id")
    row = await db.fetch_one(
        """SELECT sub.last_read_message_id
             FROM messaging.subscriptions sub
             JOIN messaging.streams s ON s.id = sub.stream_id
            WHERE sub.user_id = $1 AND s.name = $2""",
        principal.user_id, stream,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"not subscribed to {stream!r}")
    return {"stream": stream, "last_read_message_id": row["last_read_message_id"]}


@router.post("/subscriptions/cursor")
async def set_subscription_cursor(data: CursorIn, principal: Principal = Depends(get_principal)):
    """Avanca cursor monotonicamente — UPDATE so se o novo id for maior que
    o atual (evita regressao por race entre catch-up e LISTEN quase simultaneo).
    """
    if principal.user_id is None:
        raise HTTPException(status_code=401, detail="missing user_id")
    row = await db.fetch_one(
        """UPDATE messaging.subscriptions sub
              SET last_read_message_id = GREATEST(
                    COALESCE(sub.last_read_message_id, 0),
                    $3
                  )
             FROM messaging.streams s
            WHERE s.id = sub.stream_id
              AND sub.user_id = $1 AND s.name = $2
         RETURNING sub.last_read_message_id""",
        principal.user_id, data.stream, data.last_read_message_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"not subscribed to {data.stream!r}")
    return {"ok": True, "last_read_message_id": row["last_read_message_id"]}


# ---------- Users ----------

@router.post("/users")
async def create_user(data: UserIn, _: Principal = Depends(require_admin)):
    api_token = None
    if data.kind == "bot":
        api_token = secrets.token_hex(32)
    # is_active = true no UPSERT — reativa user soft-deletado (agente que
    # saiu e voltou ao agents.yaml).
    row = await db.fetch_one(
        """INSERT INTO messaging.users (email, username, full_name, kind, agent_name, api_token, is_admin, is_active)
           VALUES ($1, $2, $3, $4, $5, $6, $7, true)
           ON CONFLICT (email) DO UPDATE SET
              username = EXCLUDED.username, full_name = EXCLUDED.full_name,
              kind = EXCLUDED.kind, agent_name = EXCLUDED.agent_name,
              is_admin = EXCLUDED.is_admin,
              is_active = true,
              -- preserva api_token existente se nao rotacionou
              api_token = COALESCE(messaging.users.api_token, EXCLUDED.api_token)
           RETURNING id, email, username, full_name, kind, agent_name, api_token, is_admin, is_active""",
        data.email, data.username, data.full_name, data.kind, data.agent_name, api_token, data.is_admin,
    )
    return dict(
        id=row["id"], email=row["email"], username=row["username"],
        full_name=row["full_name"], kind=row["kind"], agent_name=row["agent_name"],
        is_admin=row["is_admin"], api_token=row["api_token"], is_active=row["is_active"],
    )


@router.get("/users")
async def list_users(_: Principal = Depends(require_admin)):
    rows = await db.fetch_all(
        """SELECT id, email, username, full_name, kind, agent_name, is_admin, is_active
             FROM messaging.users ORDER BY id""",
    )
    return [dict(r) for r in rows]


@router.patch("/users/{username}/active")
async def set_user_active(
    username: str, payload: dict, _: Principal = Depends(require_admin),
):
    """Soft-delete/reativacao de user. Body: {"is_active": bool}.

    Usado pelo reconcile pra desativar bots de agentes que sairam do
    agents.yaml. Preserva historico (messages.sender_id), mas remove o
    user de listings da UI e do '## Equipe' do system prompt dos peers.
    """
    if "is_active" not in payload or not isinstance(payload["is_active"], bool):
        raise HTTPException(status_code=400, detail="body requires {'is_active': bool}")
    row = await db.fetch_one(
        "UPDATE messaging.users SET is_active = $2 WHERE username = $1 "
        "RETURNING id, username, is_active",
        username, payload["is_active"],
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"user {username!r} does not exist")
    return {"id": row["id"], "username": row["username"], "is_active": row["is_active"]}


@router.get("/users/me")
async def whoami(principal: Principal = Depends(get_principal)):
    return dict(user_id=principal.user_id, username=principal.username, kind=principal.kind, is_admin=principal.is_admin)


@router.delete("/users/{username}")
async def delete_user(username: str, _: Principal = Depends(require_admin)):
    """Remove user do broker. Falha 409 se user ja enviou mensagens (preservar
    historico — messages.sender_id tem FK sem CASCADE). Subscriptions caem via
    CASCADE.

    Usado por reconcile pra prune de bots que sairam do agents.yaml.
    """
    row = await db.fetch_one("SELECT id, kind FROM messaging.users WHERE username = $1", username)
    if row is None:
        raise HTTPException(status_code=404, detail=f"user {username!r} does not exist")
    msg_count = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM messaging.messages WHERE sender_id = $1", row["id"],
    )
    if msg_count["n"] > 0:
        raise HTTPException(
            status_code=409,
            detail=f"user {username!r} has {msg_count['n']} message(s) in history; cannot be deleted",
        )
    await db.execute("DELETE FROM messaging.users WHERE id = $1", row["id"])
    return {"ok": True, "deleted": username}


# ---------- Pending asks ----------

@router.post("/asks")
async def create_ask(data: AskIn, principal: Principal = Depends(get_principal)):
    """Registra pending_ask. Chamado pelo agente dentro de ask_human/ask_agent.
    A conversation precisa existir (post_message deveria ter sido chamado antes
    com a pergunta em si pra historico); se nao, cria.

    D-111: `kind` distingue 'ask_human' (default — humano deve responder, push
    + Mine badge) de 'ask_agent' (target_agent deve responder, silencioso).
    Se ja existe pending_ask resolvido pra mesma conv (idempotencia em
    restart-recovery), retorna o estado atual com `resolved=true` e
    `answer_message_id` — caller usa pra detectar que ask ja foi respondido.
    """
    if principal.user_id is None:
        raise HTTPException(status_code=400, detail="pending_ask requires valid user_id (service tokens cannot ask)")
    if data.kind not in ("ask_human", "ask_agent"):
        raise HTTPException(status_code=400, detail=f"invalid kind: {data.kind!r}")
    async with db.connection() as conn:
        async with conn.transaction():
            conv_id, _ = await _get_or_create_conversation(conn, data.stream, data.topic)
            # Detecta idempotencia: se ja existe pending_ask pra essa conv
            # e ja foi resolvido (target respondeu), retorna o estado atual
            # sem sobrescrever — caller (ask_agent_via_callback apos restart)
            # le `resolved=true` e busca a resposta direto.
            existing = await conn.fetchrow(
                """SELECT conversation_id, asked_at, resolved_at, answer_message_id, kind
                     FROM messaging.pending_asks
                    WHERE conversation_id = $1""",
                conv_id,
            )
            if existing is not None and existing["resolved_at"] is not None:
                return {
                    "conversation_id": existing["conversation_id"],
                    "asked_at": existing["asked_at"].isoformat(),
                    "resolved": True,
                    "resolved_at": existing["resolved_at"].isoformat(),
                    "answer_message_id": existing["answer_message_id"],
                    "kind": existing["kind"],
                }
            row = await conn.fetchrow(
                """INSERT INTO messaging.pending_asks
                       (conversation_id, asker_id, question, context, blocking, kind, target_agent)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)
                   ON CONFLICT (conversation_id) DO UPDATE SET
                     question = EXCLUDED.question, context = EXCLUDED.context,
                     blocking = EXCLUDED.blocking, asked_at = now(),
                     kind = EXCLUDED.kind, target_agent = EXCLUDED.target_agent,
                     resolved_at = NULL, answer_message_id = NULL
                   RETURNING conversation_id, asked_at""",
                conv_id, principal.user_id, data.question, data.context, data.blocking,
                data.kind, data.target_agent,
            )
    return {
        "conversation_id": row["conversation_id"],
        "asked_at": row["asked_at"].isoformat(),
        "resolved": False,
    }


@router.get("/asks")
async def list_asks(principal: Principal = Depends(get_principal)):
    """Lista pending_asks direcionados ao humano (kind='ask_human').
    ask_agent (cross-agent) eh deliberadamente excluido — eh trafego
    silencioso pro humano (target agente responde, nao humano).
    """
    if principal.is_admin:
        rows = await db.fetch_all(
            """SELECT pa.conversation_id, s.name AS stream, c.topic_name AS topic,
                      pa.asker_id, ua.username AS asker_username,
                      pa.question, pa.context, pa.blocking, pa.asked_at
                 FROM messaging.pending_asks pa
                 JOIN messaging.conversations c ON c.id = pa.conversation_id
                 JOIN messaging.streams s ON s.id = c.stream_id
                 JOIN messaging.users ua ON ua.id = pa.asker_id
                WHERE pa.resolved_at IS NULL
                  AND pa.kind = 'ask_human'
                ORDER BY pa.asked_at DESC""",
        )
    else:
        rows = []
    return [
        dict(
            conversation_id=r["conversation_id"], stream=r["stream"], topic=r["topic"],
            asker_id=r["asker_id"], asker_username=r["asker_username"],
            question=r["question"], context=r["context"], blocking=r["blocking"],
            asked_at=r["asked_at"].isoformat(),
        ) for r in rows
    ]


# ---------- Conversations (helpers internos — aliases public no main.py) ----------
#
# Lista unica de threads, filtrada por `archived_at` na conversation (aditivo em
# migration 008). Active = IS NULL; Closed = IS NOT NULL. Filtro manual,
# controlado sempre pelo humano. Task metadata (workflow, current_step,
# current_agent, status) vem junto via LEFT JOIN em `tasks.tasks` por origin_stream/
# origin_topic — quando a conversa e origem de uma task, o frontend renderiza
# progress bar no header; senao e chat livre.

async def list_unified_conversations(
    principal: Principal,
    filter_: str = "active",
) -> list[dict]:
    """Lista conversations onde o humano esta envolvido, com task metadata opcional.
    filter_: 'active' (archived_at IS NULL) ou 'closed' (IS NOT NULL).

    Inclui:
      - conversations onde o humano ja postou (participating=true), OU
      - conversations com pending_ask nao resolvido (algum agente pediu
        atencao do humano — aparece mesmo se o humano ainda nao postou), OU
      - conversations `__ask-from-*` (sub-conversas ask_agent) — puxadas pra
        poder aninhar na sidebar sob a conv pai (vide _compute_hierarchy).

    Cada item sai com `parent_conv_id` (None se eh root) + `children_stats`
    agregado recursivamente (active, awaiting_human, resolved,
    deepest_pending_path). Convs `__ask-from-*` que nao tem pai inferivel
    ficam como root (comportamento legado: so aparecem se tiverem pending_ask).
    """
    if filter_ not in ("active", "closed"):
        raise HTTPException(status_code=400, detail="filter must be 'active' or 'closed'")
    archived_clause = "c.archived_at IS NULL" if filter_ == "active" else "c.archived_at IS NOT NULL"
    rows = await db.fetch_all(
        f"""
        SELECT c.id, c.created_at, s.name AS stream, c.topic_name AS topic,
               c.custom_title,
               c.last_message_at, c.archived_at, c.parent_conv_id AS persisted_parent_conv_id,
               (SELECT COUNT(*) FROM messaging.messages m WHERE m.conversation_id = c.id) AS msg_count,
               (SELECT m.content FROM messaging.messages m
                  WHERE m.conversation_id = c.id ORDER BY m.id DESC LIMIT 1) AS last_content,
               (SELECT u.username FROM messaging.messages m
                  JOIN messaging.users u ON u.id = m.sender_id
                  WHERE m.conversation_id = c.id ORDER BY m.id DESC LIMIT 1) AS last_sender,
               EXISTS(SELECT 1 FROM messaging.pending_asks pa
                       WHERE pa.conversation_id = c.id AND pa.resolved_at IS NULL
                         AND pa.kind = 'ask_human') AS awaiting_human,
               EXISTS(SELECT 1 FROM messaging.messages m2
                       WHERE m2.conversation_id = c.id AND m2.sender_id = $1) AS participating,
               -- D-84: modelo unificado de estado. Migration 030 introduziu
               -- messaging.runs como single source of truth — 1 row por
               -- execucao do CLI, atualizada transacionalmente pelo broker
               -- quando ingere live_events. Substituiu 3 subqueries em
               -- telemetry.live_events por um LATERAL com 1 lookup.
               r.status            AS run_status,
               r.last_heartbeat_at AS run_last_heartbeat_at,
               r.exit_reason       AS run_exit_reason,
               -- Task metadata: primeiro tenta match pela origem (conv que
               -- criou a task via primeiro complete_phase); senao tenta match
               -- por topic = 'task-<slug>' (convs intermediárias em streams
               -- de agentes que processaram alguma fase). COALESCE pra ter
               -- 1 task so, prioridade pra origem quando ambos batem.
               COALESCE(t.slug,          t_topic.slug)          AS task_slug,
               COALESCE(t.title,         t_topic.title)         AS task_title,
               COALESCE(t.workflow,      t_topic.workflow)      AS task_workflow,
               COALESCE(t.status,        t_topic.status)        AS task_status,
               COALESCE(t.current_step,  t_topic.current_step)  AS task_current_step,
               COALESCE(t.current_agent, t_topic.current_agent) AS task_current_agent,
               COALESCE(t.complexity,    t_topic.complexity)    AS task_complexity,
               -- true quando esta conv eh a ORIGEM da task (match via
               -- origin_stream/origin_topic). Usado em _compute_hierarchy
               -- pra escolher o pai de convs irmas task-<slug> de forma
               -- deterministica — sem isso, a ordem DESC por last_message_at
               -- pode fazer uma conv filha sobrescrever a origem.
               (t.slug IS NOT NULL)                              AS is_task_origin,
               -- Quando a task task_current_agent != this.stream, esta conv
               -- (filha intermediaria task-<slug> em stream diferente do
               -- agente atual da fase) ja concluiu sua parte — o frontend
               -- usa isso pra considerar a filha como resolved e nao
               -- contar em children_stats.active (evita "RUNNING" fantasma).
               CASE
                 WHEN COALESCE(t.current_agent, t_topic.current_agent) IS NOT NULL
                  AND COALESCE(t.current_agent, t_topic.current_agent) != s.name
                 THEN true
                 ELSE false
               END AS task_not_current_agent
          FROM messaging.conversations c
          JOIN messaging.streams s ON s.id = c.stream_id
          LEFT JOIN LATERAL (
                SELECT status, last_heartbeat_at, exit_reason
                  FROM messaging.runs
                 WHERE conversation_id = c.id
                 ORDER BY started_at DESC LIMIT 1
          ) r ON true
          LEFT JOIN tasks.tasks t
                 ON t.origin_stream = s.name
                AND t.origin_topic = c.topic_name
                AND t.archived_at IS NULL
          LEFT JOIN tasks.tasks t_topic
                 ON c.topic_name = 'task-' || t_topic.slug
                AND t_topic.archived_at IS NULL
         WHERE {archived_clause}
           AND (
                 EXISTS (
                   SELECT 1 FROM messaging.messages m3
                    WHERE m3.conversation_id = c.id AND m3.sender_id = $1
                 )
              OR EXISTS (
                   -- D-111: ask_agent (kind='ask_agent') nao puxa conv pra
                   -- lista do humano — eh agente-pra-agente, silencioso.
                   SELECT 1 FROM messaging.pending_asks pa2
                    WHERE pa2.conversation_id = c.id AND pa2.resolved_at IS NULL
                      AND pa2.kind = 'ask_human'
                 )
              OR c.topic_name LIKE '\\_\\_ask-from-%' ESCAPE '\\'
              OR c.topic_name LIKE '\\_\\_child-%' ESCAPE '\\'
              -- Convs de handoff entre agentes (topic = 'task-<slug>'
              -- em streams secundarias) precisam entrar aqui pra servirem
              -- de "elo do meio" na arvore — caso contrario filhos ask_agent
              -- do target apontam pra um parent_conv_id que nao esta em
              -- byId e viram root na sidebar. Ver D-77 followup.
              OR EXISTS (
                   SELECT 1 FROM tasks.tasks t2
                    WHERE c.topic_name = 'task-' || t2.slug
                      AND t2.archived_at IS NULL
                 )
               )
         ORDER BY c.last_message_at DESC
         LIMIT 200
        """,
        principal.user_id or 0,
    )
    out = []
    for r in rows:
        # D-96 cleanup: `ask_replied` (D-76) e `is_completed_child` (D-93)
        # foram removidos. Toda filha (parent_conv_id != null) eh read-only
        # pro humano. Frontend deriva read-only direto de parent_conv_id.
        # _compute_hierarchy usa is_running/is_stuck/awaiting_human pra
        # decidir bucket active/resolved.

        # D-84: modelo unificado de estado — 4 sinais flat, mutuamente
        # exclusivos por construcao via precedencia:
        #   awaiting_human > is_stuck > is_running > is_errored > idle
        # Fonte (migration 030): messaging.runs.status. Reaper transiciona
        # running -> stale quando heartbeat fica obsoleto, entao stuck
        # vira automatico quando elapsed > RUNNER_STUCK_SEC mesmo antes
        # do reaper rodar (cobre janela entre heartbeat antigo e proxima
        # passada do scheduler).
        awaiting_human = bool(r["awaiting_human"])
        run_status = r["run_status"]
        heartbeat_at = r["run_last_heartbeat_at"]
        elapsed: float | None = None
        if heartbeat_at is not None:
            elapsed = (datetime.now(tz=timezone.utc) - heartbeat_at).total_seconds()
        is_stuck = bool(
            run_status == "running"
            and elapsed is not None
            and elapsed > RUNNER_STUCK_SEC
            and not awaiting_human
        )
        is_running = bool(
            run_status == "running"
            and not is_stuck
            and not awaiting_human
        )
        is_errored = bool(
            run_status == "error"
            and not awaiting_human
        )
        item = dict(
            id=r["id"], stream=r["stream"], topic=r["topic"],
            custom_title=r["custom_title"],
            created_at=r["created_at"].isoformat() if r["created_at"] else None,
            last_message_at=r["last_message_at"].isoformat(),
            archived_at=r["archived_at"].isoformat() if r["archived_at"] else None,
            msg_count=r["msg_count"], last_content=r["last_content"], last_sender=r["last_sender"],
            awaiting_human=awaiting_human,
            participating=r["participating"],
            is_running=is_running,
            is_stuck=is_stuck,
            is_errored=is_errored,
        )
        if r["task_slug"]:
            item["task"] = dict(
                slug=r["task_slug"],
                title=r["task_title"],
                workflow=r["task_workflow"],
                status=r["task_status"],
                current_step=r["task_current_step"],
                current_agent=r["task_current_agent"],
                complexity=r["task_complexity"],
            )
        # D-79: sinal de "filha intermediaria ja concluida" — usado pelo
        # isResolvedSelf do store pra nao contar convs `task-<slug>` de
        # fases antigas como children_active do pai (o agente atual esta
        # em outro stream, esta filha nao vai rodar mais nessa task).
        item["task_not_current_agent"] = bool(r["task_not_current_agent"])
        # D-87: parent_conv_id persistido no schema — pre-preenche item.
        # `_compute_hierarchy` usa diretamente se presente; heuristica antiga
        # vira fallback so pra convs legacy (pre-migration) com NULL.
        item["_persisted_parent_conv_id"] = r["persisted_parent_conv_id"]
        # Usado em _compute_hierarchy pra ancorar o pai certo quando varias
        # convs compartilham topic = 'task-<slug>'. So uma delas eh a origem
        # real (JOIN via origin_stream/origin_topic no SQL acima).
        item["_is_task_origin"] = bool(r["is_task_origin"])
        out.append(item)

    # Calcula pai-filha + stats agregados. Sem schema novo: deduzido de
    # topic patterns + telemetry.live_events + tasks.tasks. Ver helper.
    await _compute_hierarchy(out)
    # Campos internos consumidos so por _compute_hierarchy — nao vazam pro JSON.
    for it in out:
        it.pop("_is_task_origin", None)
        it.pop("_persisted_parent_conv_id", None)
    return out


async def _compute_hierarchy(items: list[dict]) -> None:
    """Anota cada item com `parent_conv_id` e `children_stats`.

    D-96 cleanup: heuristica antiga (Fase 1 ask_agent tool_use parsing,
    Fase 2 task.slug siblings inference, deteccao de ciclos) removida —
    fonte unica eh `messaging.conversations.parent_conv_id` persistido
    via SQL (D-87 + reactor handoffs). Convs legacy pre-D-87 sem o
    campo viram root visualmente — aceitavel.

    Profundidade maxima eh 1 por construcao (D-96 broker rejeita 409
    em depth violation), entao o agregado eh trivial: 1 nivel de filhos
    por raiz, sem recursao.

    children_stats:
      - active: filhas ativas (running/stuck/awaiting_human, ou
        task nao-terminal sem completed_child)
      - stuck: filhas com is_stuck
      - resolved: filhas que ja terminaram seu turno
    """
    if not items:
        return
    by_id: dict[int, dict] = {it["id"]: it for it in items}

    # Anota parent_conv_id direto do schema. Convs cujo pai nao esta nos
    # items carregados ficam orfas (parent_conv_id None na resposta) —
    # listagem nao mostra hierarquia entre raizes ja arquivadas.
    for it in items:
        pid = it.get("_persisted_parent_conv_id")
        it["parent_conv_id"] = pid if (pid is not None and pid != it["id"] and pid in by_id) else None
        it["children_stats"] = {"active": 0, "stuck": 0, "resolved": 0}

    def _is_resolved(it: dict) -> bool:
        # Resolvida = filha que ja terminou seu turno e nao esta fazendo
        # nada agora. Criterios:
        #   - awaiting_human / is_running / is_stuck → ainda ativa
        #   - task em status terminal (done/halt/human_review) → resolvida
        #   - task_not_current_agent (D-79) → fase concluida, resolvida
        #   - filha (parent_conv_id != null) ociosa → resolvida
        if it.get("awaiting_human"): return False
        if it.get("is_running"): return False
        if it.get("is_stuck"): return False
        task = it.get("task")
        if task and task.get("status") in ("done", "halt", "human_review"):
            return True
        if it.get("task_not_current_agent"):
            return True
        if it["parent_conv_id"] is not None:
            return True
        return False

    # Agregado de 1 nivel: pra cada filha, contribui no children_stats do pai.
    for child in items:
        pid = child["parent_conv_id"]
        if pid is None:
            continue
        parent = by_id.get(pid)
        if parent is None:
            continue
        stats = parent["children_stats"]
        if _is_resolved(child):
            stats["resolved"] += 1
        else:
            stats["active"] += 1
        if child.get("is_stuck"):
            stats["stuck"] += 1


async def delete_conversation(conv_id: int, principal: Principal = Depends(get_principal)):
    """Delete permanente. Cascade cuida de messages/attachments/pending_asks/
    closed_conversations/read_cursors (ver 001_init.sql FKs ON DELETE CASCADE).

    Sem gate admin: hoje todo usuario logado eh admin de fato. Registrado em
    QUESTIONS como melhoria futura (flag/coluna admin)."""
    if principal.user_id is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    result = await db.execute(
        "DELETE FROM messaging.conversations WHERE id = $1",
        conv_id,
    )
    return {"ok": True, "result": result}


async def close_conversation(conv_id: int, principal: Principal = Depends(get_principal)):
    if principal.user_id is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    # DO UPDATE pra renovar `closed_at` em closes repetidos. Combinado com a
    # regra de `closed_at >= last_message_at` (D-28), garante que fechar uma
    # conv previamente fechada + reaberta por msg nova volta a escondela.
    await db.execute(
        """INSERT INTO web.closed_conversations (conversation_id, user_id, closed_at)
           VALUES ($1, $2, now())
           ON CONFLICT (conversation_id, user_id)
           DO UPDATE SET closed_at = EXCLUDED.closed_at""",
        conv_id, principal.user_id,
    )
    return {"ok": True}


async def reopen_conversation(conv_id: int, principal: Principal = Depends(get_principal)):
    if principal.user_id is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    await db.execute(
        "DELETE FROM web.closed_conversations WHERE conversation_id = $1 AND user_id = $2",
        conv_id, principal.user_id,
    )
    return {"ok": True}


async def close_all_conversations(principal: Principal = Depends(get_principal)):
    """Marca como fechadas TODAS as conversas visiveis do usuario exceto as com ask pendente."""
    if principal.user_id is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    # DO UPDATE renova `closed_at` (ver close_conversation) pra respeitar
    # regra `closed_at >= last_message_at`.
    result = await db.execute(
        """
        INSERT INTO web.closed_conversations (conversation_id, user_id, closed_at)
        SELECT c.id, $1, now() FROM messaging.conversations c
         WHERE NOT EXISTS (
                 -- D-111: humano so eh bloqueado de fechar por ask_human.
                 -- ask_agent nao impede close (cross-agent, silencioso).
                 SELECT 1 FROM messaging.pending_asks pa
                  WHERE pa.conversation_id = c.id AND pa.resolved_at IS NULL
                    AND pa.kind = 'ask_human'
               )
        ON CONFLICT (conversation_id, user_id)
        DO UPDATE SET closed_at = EXCLUDED.closed_at
        """,
        principal.user_id,
    )
    return {"ok": True, "result": result}


# ---------- Scheduler proxy (PWA <-> scheduler container) ----------
#
# O scheduler expoe sua propria mini-API HTTP (orchestrator/scheduler_http.py)
# na rede compose, sem auth — porta nao publicada no host. Aqui no broker
# proxiamos com auth pra UI consumir. Read-only via session normal; acoes
# (run/pause/resume) exigem admin.

SCHEDULER_URL = os.environ.get("SCHEDULER_URL", "http://scheduler:8811").rstrip("/")


async def _scheduler_request(method: str, path: str, *, timeout: float = 10.0) -> tuple[int, Any]:
    """Faz request ao scheduler e devolve (status, parsed_body). body pode ser
    dict, list ou None (204)."""
    url = f"{SCHEDULER_URL}{path}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as sess:
            async with sess.request(method, url) as r:
                if r.status == 204:
                    return r.status, None
                ct = (r.headers.get("Content-Type") or "").lower()
                if "json" in ct:
                    return r.status, await r.json()
                return r.status, {"raw": (await r.text())[:500]}
    except aiohttp.ClientError as e:
        raise HTTPException(status_code=502, detail=f"scheduler unreachable: {e}")
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="scheduler timeout")


@router.get("/scheduler/health")
async def scheduler_health(_: Principal = Depends(get_principal)):
    """Estado agregado do scheduler (uptime, contadores). Proxy de :8811/health."""
    status, body = await _scheduler_request("GET", "/health")
    if status >= 500:
        raise HTTPException(status_code=status, detail=body)
    return body


@router.get("/scheduler/jobs")
async def scheduler_jobs(_: Principal = Depends(get_principal)):
    """Lista jobs registrados com next_run_time, status pause, ultima execucao."""
    status, body = await _scheduler_request("GET", "/jobs")
    if status >= 400:
        raise HTTPException(status_code=status, detail=body)
    return body


@router.post("/scheduler/jobs/{job_id}/run")
async def scheduler_run_job(job_id: str, _: Principal = Depends(require_admin)):
    """Dispara dispatch_job(job) ad-hoc no scheduler. Retorna 202 imediato; o
    job roda em background (backups demoram alguns minutos)."""
    status, body = await _scheduler_request("POST", f"/jobs/{job_id}/run", timeout=15.0)
    if status >= 400:
        raise HTTPException(status_code=status, detail=body)
    return body


@router.post("/scheduler/jobs/{job_id}/pause")
async def scheduler_pause_job(job_id: str, _: Principal = Depends(require_admin)):
    """Pausa job — runtime-only, some em restart do scheduler."""
    status, body = await _scheduler_request("POST", f"/jobs/{job_id}/pause")
    if status >= 400:
        raise HTTPException(status_code=status, detail=body or "pause failed")
    return {"ok": True}


@router.post("/scheduler/jobs/{job_id}/resume")
async def scheduler_resume_job(job_id: str, _: Principal = Depends(require_admin)):
    """Resume job pausado."""
    status, body = await _scheduler_request("POST", f"/jobs/{job_id}/resume")
    if status >= 400:
        raise HTTPException(status_code=status, detail=body or "resume failed")
    return {"ok": True}
