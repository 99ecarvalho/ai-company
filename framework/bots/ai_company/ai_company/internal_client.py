"""Client interno pra broker HTTP.

- Envio de mensagens: POST /api/messages no broker (HTTP).
- Recebimento de mensagens: Postgres LISTEN direto (pg_notify dispara em cada
  INSERT em messaging.messages via trigger).
- Eventos sao convertidos para um shape padronizado (dict com type, sender_id,
  sender_full_name, sender_email, subject, display_recipient, content, id)
  consumido pelo Dispatcher/SessionManager.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, AsyncIterator

import aiohttp
import asyncpg

from .log import get_logger


log = get_logger(__name__)


# Linux filesystem caps filename at 255 bytes. Reserve headroom for suffixes the
# framework appends (e.g. `.session_state.json`) and unicode multibyte chars.
SLUG_MAX_BYTES = 200


@dataclass(frozen=True)
class TopicKey:
    stream: str
    topic: str

    def slug(self) -> str:
        def safe(s: str) -> str:
            return "".join(c if c.isalnum() or c in "-_" else "_" for c in s).strip("_")
        stream_safe = safe(self.stream)
        topic_safe = safe(self.topic)
        full = f"{stream_safe}__{topic_safe}"
        if len(full.encode("utf-8")) <= SLUG_MAX_BYTES:
            return full
        # Topic muito longo (ex: descrição inteira passada como next_topic).
        # Trunca byte-safe e anexa hash curto pra manter slug determinístico e único.
        digest = hashlib.sha1(self.topic.encode("utf-8")).hexdigest()[:10]
        # Reserva: stream_safe + "__" + "__" + digest
        reserved = len(stream_safe.encode("utf-8")) + 2 + 2 + len(digest)
        keep_bytes = max(10, SLUG_MAX_BYTES - reserved)
        topic_bytes = topic_safe.encode("utf-8")[:keep_bytes]
        # Remove um último byte parcial de multibyte (decode com 'ignore' corta no ponto certo)
        topic_trunc = topic_bytes.decode("utf-8", errors="ignore").rstrip("_")
        return f"{stream_safe}__{topic_trunc}__{digest}"


class InternalClient:
    """Cliente HTTP do broker interno (envio + LISTEN/NOTIFY pra eventos)."""

    def __init__(self, broker_url: str, token: str, streams: list[str],
                 database_url: str | None = None):
        self._broker = broker_url.rstrip("/")
        self._token = token
        self._streams = streams
        self._database_url = database_url or os.environ["DATABASE_URL"]
        self._profile: dict[str, Any] = {}
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._stream_id_by_name: dict[str, int] = {}
        self._subscribed_convs: set[int] = set()
        self._listen_conn: asyncpg.Connection | None = None
        # Serializa chamadas em `_listen_conn` — asyncpg nao permite
        # operacoes concorrentes na mesma conexao (stmt_exclusive_section).
        # Sem este lock, N `add_listener` em paralelo (ex: ask_agents_many)
        # derrubam todos menos um com InterfaceError, deixando o agente
        # asker surdo pras respostas daquelas convs.
        self._listen_lock = asyncio.Lock()
        self._listener_task: asyncio.Task | None = None
        self._http: aiohttp.ClientSession | None = None
        self._stopped = asyncio.Event()

    @property
    def owned_streams(self) -> list[str]:
        """Streams que este agente assina (fonte de eventos dele).
        Usado pelo dispatcher pra filtrar eventos que chegam via
        `subscribe_to_conversation` mas sao de conv filha em outro
        stream — esses so devem destravar ask_agent/ask_human, nao
        virar eventos enfileirados no topic loop local (D-75)."""
        return list(self._streams)

    @property
    def user_id(self) -> int:
        return int(self._profile.get("user_id", 0))

    @property
    def email(self) -> str:
        return str(self._profile.get("email", ""))

    @property
    def full_name(self) -> str:
        return str(self._profile.get("full_name", self._profile.get("username", "")))

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def start(self) -> None:
        self._http = aiohttp.ClientSession(headers=self._headers)
        # /api/users/me
        async with self._http.get(f"{self._broker}/api/users/me") as r:
            r.raise_for_status()
            me = await r.json()
            self._profile = {
                "user_id": me.get("user_id"),
                "email": me.get("username") + "@internal.ai-company",
                "full_name": me.get("username"),
                "username": me.get("username"),
            }
        # stream IDs (pros LISTEN channels)
        async with self._http.get(f"{self._broker}/api/streams") as r:
            r.raise_for_status()
            resp = await r.json()
            streams = resp.get("streams", []) if isinstance(resp, dict) else resp
            for s in streams:
                self._stream_id_by_name[s["name"]] = s["id"]
        # Catch-up de unread (D-52) ANTES do LISTEN. Pra cada stream, busca
        # cursor persistido (last_read_message_id) e replaya msgs faltantes
        # do broker. Em seguida ativa o LISTEN — janela entre o ultimo replay
        # e o add_listener eh desprezivel (um UPDATE de cursor + add_listener).
        # pg_notify nao seria entregue enquanto o LISTEN nao esta ativo, entao
        # msgs novas nesse intervalo tambem entram no replay na proxima vez.
        await self._catch_up_unread()

        # Inicia LISTEN em cada stream configurado
        self._listen_conn = await asyncpg.connect(self._database_url)
        for stream in self._streams:
            sid = self._stream_id_by_name.get(stream)
            if sid is None:
                log.warning("internal_client.stream_not_found", stream=stream)
                continue
            await self._listen_conn.add_listener(f"msg_stream_{sid}", self._on_notify)
        # LISTEN no canal global `agent_ctrl` pra eventos de controle (cancel etc).
        # Payload eh JSON com `stream` — filtramos em `_on_ctrl_notify` pros
        # streams que este agente escuta.
        await self._listen_conn.add_listener("agent_ctrl", self._on_ctrl_notify)
        log.info(
            "internal_client.started",
            user_id=self.user_id, email=self.email,
            streams=[(s, self._stream_id_by_name.get(s)) for s in self._streams],
        )

    async def _catch_up_unread(self) -> None:
        """Replaya mensagens que chegaram enquanto o bot esteve down.

        Pra cada stream inscrito, busca cursor (`GET /api/subscriptions/cursor`)
        e pede msgs `id > last_read` via `GET /api/messages?stream&since_id`.
        Cada msg vira evento no mesmo shape do LISTEN path e entra no pipeline.
        Cursor avanca ao fim do replay de cada stream.

        Em primeira subida (cursor NULL), setamos pro max(id) atual do stream
        SEM replayar — evita avalanche de replays de todo o historico na
        instalacao. Catch-up genuino acontece da 2a subida em diante.
        """
        if self._http is None:
            return
        for stream in self._streams:
            sid = self._stream_id_by_name.get(stream)
            if sid is None:
                continue
            try:
                async with self._http.get(
                    f"{self._broker}/api/subscriptions/cursor",
                    params={"stream": stream},
                ) as r:
                    if r.status == 404:
                        # subscription ainda nao existe — reconcile cria, mas em
                        # race de boot pode nao ter sido aplicada. Pula; proxima
                        # vez pega.
                        log.warning("internal_client.cursor_sub_missing", stream=stream)
                        continue
                    r.raise_for_status()
                    cursor_body = await r.json()
                    last_read = cursor_body.get("last_read_message_id")
            except Exception:
                log.exception("internal_client.cursor_fetch_failed", stream=stream)
                continue

            if last_read is None:
                # Primeira subida: semeia cursor com a ultima msg do stream
                # e nao replaya. Evita avalanche de N meses de historico.
                max_id = await self._fetch_max_stream_id(stream)
                if max_id > 0:
                    await self._save_cursor(stream, max_id)
                log.info("internal_client.cursor_seeded", stream=stream, last_read=max_id)
                continue

            # Replay: pagina ate esvaziar. Limit 500 por request; loop incrementa.
            replayed = 0
            page_cursor = last_read
            while True:
                try:
                    async with self._http.get(
                        f"{self._broker}/api/messages",
                        params={"stream": stream, "since_id": page_cursor, "limit": 500},
                    ) as r:
                        r.raise_for_status()
                        rows = await r.json()
                except Exception:
                    log.exception("internal_client.replay_fetch_failed", stream=stream)
                    break
                if not rows:
                    break
                for m in rows:
                    # Ignora msgs proprias (bot nao consome o que ele mesmo postou)
                    if m.get("sender_id") == self.user_id:
                        page_cursor = max(page_cursor, m["id"])
                        continue
                    # Migration 026: catch-up tambem ignora echoes (mesma
                    # justificativa do _on_notify) — cursor avanca pra nao
                    # ficar em loop de replay.
                    if m.get("kind", "regular") != "regular":
                        page_cursor = max(page_cursor, m["id"])
                        continue
                    event = self._event_from_message(m)
                    await self._queue.put(event)
                    replayed += 1
                    page_cursor = max(page_cursor, m["id"])
                if len(rows) < 500:
                    break
            if page_cursor > last_read:
                await self._save_cursor(stream, page_cursor)
            if replayed:
                log.info(
                    "internal_client.catch_up_replayed",
                    stream=stream, replayed=replayed, cursor=page_cursor,
                )

    async def _fetch_max_stream_id(self, stream: str) -> int:
        """Descobre max(id) do stream consultando /api/messages com since=0 limit=1
        ordenado DESC (o endpoint retorna ASC, entao fazemos loop com salto).
        Solucao simples: busca limit=1 partindo de since_id=0 e assume 1 pagina;
        pra stream com historico vasto, seeding com max=0 tbm eh aceitavel
        (so significa que vai replayar tudo no proximo start — ja eh o
        comportamento que queremos evitar; raro em producao porque o seed
        ocorre exatamente na instalacao). Trade-off aceito pra nao criar
        endpoint novo so pra isso.
        """
        if self._http is None:
            return 0
        # Bound otimista: pega 500 msgs e pega o max id. Na primeira subida
        # isso eh 0 (stream vazio) ou <=500. Raro ter mais.
        try:
            async with self._http.get(
                f"{self._broker}/api/messages",
                params={"stream": stream, "since_id": 0, "limit": 500},
            ) as r:
                if r.status != 200:
                    return 0
                rows = await r.json()
        except Exception:
            return 0
        if not rows:
            return 0
        return max(m["id"] for m in rows)

    async def _save_cursor(self, stream: str, message_id: int) -> None:
        if self._http is None:
            return
        try:
            async with self._http.post(
                f"{self._broker}/api/subscriptions/cursor",
                json={"stream": stream, "last_read_message_id": message_id},
            ) as r:
                if r.status >= 400:
                    log.warning(
                        "internal_client.cursor_save_failed",
                        stream=stream, message_id=message_id, status=r.status,
                    )
        except Exception:
            log.exception("internal_client.cursor_save_exc", stream=stream)

    def mark_processed(self, stream: str, message_id: int) -> None:
        """Avanca cursor pra `message_id` depois que o dispatcher terminou
        de processar o turno daquela msg (D-78). Fire-and-forget.

        Se chamado varias vezes pro mesmo stream, o broker faz
        `GREATEST(atual, novo)` — cursor nunca regride. Se stream nao e
        dos assinados por este agente, no-op.
        """
        if not stream or stream not in self._streams:
            return
        asyncio.create_task(self._save_cursor(stream, message_id))

    def _event_from_message(self, m: dict) -> dict:
        """Converte linha de /api/messages no shape de evento do Dispatcher.
        Mesma forma que _enrich_and_enqueue monta pra notifies — centralizado
        aqui pra reuso pelo catch-up.

        D-87: inclui `conversation_id` pra que claude_runner.handle possa
        propagar ao McpBroker via register_topic — necessário pro handler
        MCP de ask_agent passar parent_conv_id na criação da conv filha.
        """
        return {
            "id": m["id"],
            "conversation_id": m.get("conversation_id"),
            "type": "stream",
            "sender_id": m["sender_id"],
            "sender_full_name": m["sender_username"],
            "sender_email": f"{m['sender_username']}@internal.ai-company",
            "sender_is_bot": False,
            "subject": m["topic"],
            "display_recipient": m["stream"],
            "content": m["content"],
            "timestamp": m["sent_at"],
        }

    def stop(self) -> None:
        self._stopped.set()

    def _on_notify(self, _conn, _pid, _channel: str, payload: str) -> None:
        """Callback do LISTEN — roda no loop do asyncpg. Enriquece e enfileira."""
        try:
            data = json.loads(payload)
            # Filtro: ignora msgs proprias
            if data.get("sender_id") == self.user_id:
                return
            # Migration 026: ignora kind != 'regular' — echoes (D-100 forward
            # de reply de conv-filha) sao puramente visuais, nao acordam o
            # runner. Sem isso, o agente parent ganhava um turn extra
            # redundante toda vez que um agente filho respondia (caso
            # observado em 2026-04-28: PO escreveu "tres opcoes" e
            # 13s depois acordou pra escrever "ja apresentei as tres
            # opcoes" — gerado pelo eco do reply do executor).
            if data.get("kind", "regular") != "regular":
                return
            asyncio.create_task(self._enrich_and_enqueue(data))
        except Exception:
            log.exception("internal_client.notify_parse_failed", payload=payload[:200])

    def _on_ctrl_notify(self, _conn, _pid, _channel: str, payload: str) -> None:
        """Callback do LISTEN `agent_ctrl` — eventos de controle do framework
        (cancel_topic etc). Filtra pelo stream que o agente escuta."""
        try:
            data = json.loads(payload)
            stream = data.get("stream")
            if stream not in self._streams:
                return
            ctrl_event = {
                "_ctrl": True,
                "ctrl_type": data.get("type"),
                "display_recipient": stream,
                "subject": data.get("topic"),
                "user_id": data.get("user_id"),
                "silent": bool(data.get("silent", False)),
            }
            # Enfileira direto (sem enrichment — ja temos tudo no payload).
            self._queue.put_nowait(ctrl_event)
        except Exception:
            log.exception("internal_client.ctrl_notify_failed", payload=payload[:200])

    async def _enrich_and_enqueue(self, data: dict) -> None:
        """Busca stream/topic/sender metadata via HTTP (cache-friendly no futuro) e
        converte pra shape de evento padronizado consumido pelo Dispatcher."""
        if self._http is None:
            return
        conv_id = data["conversation_id"]
        async with self._http.get(
            f"{self._broker}/api/messages",
            params={"conversation_id": conv_id, "since_id": data["id"] - 1, "limit": 1},
        ) as r:
            if r.status != 200:
                log.warning("internal_client.enrich_failed", status=r.status)
                return
            rows = await r.json()
            if not rows:
                return
            m = rows[0]
        event = self._event_from_message(m)
        await self._queue.put(event)
        # D-78: cursor NAO avanca aqui (pre-D-78 avancava pra pular replay de
        # msgs ja enfileiradas). Problema: se o container caisse com msg na
        # queue in-memory, cursor estava adiante → catch-up no proximo start
        # nao replayava → msg perdida. Agora o dispatcher chama
        # `mark_processed` apos handler.handle terminar (em _run_turn),
        # garantindo que so avanca depois de turn concluido. Replay duplicado
        # no restart e tolerado: dispatcher re-enfileira, claude_runner
        # resume pelo session_id, Claude ignora ou reprocessa — preferivel a
        # perder a msg.

    async def subscribe_to_conversation(self, conversation_id: int) -> bool:
        """LISTEN no canal `msg_conv_<conversation_id>` — escuta apenas
        mensagens dessa conversa especifica. Usado por ask_agent pra aguardar
        resposta do target SEM over-subscribe no stream inteiro (bug D-50).
        Idempotente: no-op se ja subscrito.
        """
        if self._listen_conn is None:
            return False
        if conversation_id in self._subscribed_convs:
            return True
        try:
            async with self._listen_lock:
                # Re-check inside the lock — evita double-add quando duas
                # chamadas com o mesmo conv_id correm em paralelo.
                if conversation_id in self._subscribed_convs:
                    return True
                await self._listen_conn.add_listener(
                    f"msg_conv_{conversation_id}", self._on_notify
                )
                self._subscribed_convs.add(conversation_id)
            log.info("internal_client.subscribed_conv", conversation_id=conversation_id)
            return True
        except Exception:
            log.exception(
                "internal_client.subscribe_conv_failed", conversation_id=conversation_id
            )
            return False

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while not self._stopped.is_set():
            try:
                ev = await asyncio.wait_for(self._queue.get(), timeout=5.0)
                yield ev
            except asyncio.TimeoutError:
                continue

    async def send_message(
        self,
        stream: str,
        topic: str,
        content: str,
        parent_conv_id: int | None = None,
        client_id: str | None = None,
        kind: str = "regular",
    ) -> dict[str, Any]:
        """D-87: `parent_conv_id` passa pro broker, que persiste na coluna
        homonima de messaging.conversations quando cria a conv (primeira
        msg em (stream, topic)). Hierarquia vira lookup O(1) via FK.

        `client_id`: idempotency key (UNIQUE per conversation_id). Permite
        re-envio seguro do mesmo conteudo (race, retry, double-fire de
        D-100 echo) sem criar mensagens duplicadas. Opcional — None mantem
        comportamento legado.

        `kind` (migration 026): 'regular' (default) dispara turn no
        listener do agente. 'echo' eh forward visual de reply de
        conv-filha (D-100) — fica visivel no PWA mas nao acorda runner
        do agente parent."""
        assert self._http is not None
        body_json: dict[str, Any] = {"stream": stream, "topic": topic, "content": content}
        if parent_conv_id is not None:
            body_json["parent_conv_id"] = parent_conv_id
        if client_id is not None:
            body_json["client_id"] = client_id
        if kind != "regular":
            body_json["kind"] = kind
        async with self._http.post(
            f"{self._broker}/api/messages",
            json=body_json,
        ) as r:
            if r.status >= 400:
                body = await r.text()
                raise RuntimeError(f"send_message failed: HTTP {r.status} {body[:200]}")
            return await r.json()

    async def update_message(self, message_id: int, content: str) -> dict[str, Any]:
        # Broker atual nao suporta edicao. No-op preservado pra compatibilidade
        # de assinatura (callers podem invocar mas nao ha efeito).
        log.warning("internal_client.update_message_noop", message_id=message_id)
        return {"result": "success"}

    async def archive_conversation(self, conv_id: int) -> dict[str, Any]:
        """POST /api/conversations/<id>/archive. Backend faz cascade em
        descendentes + cancel de runners ativos (D-72)."""
        assert self._http is not None
        async with self._http.post(
            f"{self._broker}/api/conversations/{conv_id}/archive",
        ) as r:
            if r.status >= 400:
                body = await r.text()
                raise RuntimeError(
                    f"archive_conversation failed: HTTP {r.status} {body[:200]}"
                )
            return await r.json()

    async def create_pending_ask(
        self,
        stream: str,
        topic: str,
        question: str,
        context: str = "",
        blocking: bool = True,
        kind: str = "ask_human",
        target_agent: str | None = None,
    ) -> dict[str, Any]:
        """Registra pending_ask no broker. Essencial pra ask_human/ask_agent —
        sem isso, o humano nao ve a conversa em Mine e o push_notifier nao
        dispara. Chamado depois do send_message da pergunta.

        D-111: `kind='ask_agent'` persiste pra restart-recovery (broker auto-
        resolve quando target responde). UI/push filtram por `kind='ask_human'`
        pra nao alertar humano de asks agente-pra-agente.

        Resposta inclui `resolved: bool` — se true, ask ja foi respondido
        (caso restart-recovery onde target respondeu antes do asker voltar);
        caller le `answer_message_id` pra buscar a resposta direto."""
        assert self._http is not None
        async with self._http.post(
            f"{self._broker}/api/asks",
            json={
                "stream": stream,
                "topic": topic,
                "question": question,
                "context": context,
                "blocking": blocking,
                "kind": kind,
                "target_agent": target_agent,
            },
        ) as r:
            if r.status >= 400:
                body = await r.text()
                raise RuntimeError(f"create_pending_ask failed: HTTP {r.status} {body[:200]}")
            return await r.json()
