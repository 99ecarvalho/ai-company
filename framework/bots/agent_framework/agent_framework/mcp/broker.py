"""McpBroker — estado compartilhado entre o servidor MCP e o BaseAgent.

Responsabilidades:
  - Manter um dict topic_slug -> TopicKey (o MCP resolve URLs por slug)
  - Gerenciar asyncio.Future por topic_key pra asks pendentes
  - Persistir perguntas em `pending_questions/<slug>.json` pra crash recovery
  - Expor callbacks que o BaseAgent usa pra postar perguntas no broker
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from ..internal_client import TopicKey
from ..log import get_logger

log = get_logger(__name__)

# callback: (topic, question, context, blocking) -> awaitable de Nada
OnAskCallback = Callable[[TopicKey, str, str, bool], Awaitable[None]]

# callback pra notify_human: (topic, message) -> awaitable. Posta msg no
# broker sem criar pending_ask. Comportamento diferente de ask_human:
# nao aguarda, nao bloqueia, nao vira waiting badge na UI.
OnNotifyCallback = Callable[[TopicKey, str], Awaitable[None]]

# callback pra archive_conversation: (conv_id) -> awaitable. POSTa
# /api/conversations/<id>/archive no broker. Erro deve subir como excecao
# pro handler MCP renderizar pro agente.
OnArchiveCallback = Callable[[int], Awaitable[None]]

# callback pra ask_agent: (asker_topic, from_agent, target_agent, question, context)
# -> tupla (TopicKey, resposta_se_ja_resolvido).
#   - TopicKey: target onde a msg foi postada (ou re-encontrada).
#   - resposta_se_ja_resolvido: D-111 restart-recovery — se pending_ask
#     dessa conv ja foi resolvido por uma resposta do target enquanto o
#     asker estava down, retorna o conteudo direto (sem repostar nem
#     aguardar). None = caminho normal (caller aguarda Future).
# asker_topic eh o topic atual de quem esta chamando (None se nao mapeavel) —
# usado pra cycle/depth check ao montar o nome do topic do target.
OnAskAgentCallback = Callable[
    [TopicKey | None, str, str, str, str], Awaitable[tuple[TopicKey, str | None]]
]


class McpBroker:
    def __init__(self, pending_dir: Path):
        self.pending_dir = pending_dir
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self._pending_futures: dict[TopicKey, asyncio.Future[str]] = {}
        self._slug_to_key: dict[str, TopicKey] = {}
        # D-87: conv_id indexado por slug. Populado pelo Dispatcher quando
        # processa cada evento. Usado pelo MCP handler de `__ask_agent` pra
        # passar parent_conv_id ao broker no create-conv da nova `__ask-from-*`.
        self._slug_to_conv_id: dict[str, int] = {}
        self._on_ask: OnAskCallback | None = None
        self._on_ask_agent: OnAskAgentCallback | None = None
        self._on_notify: OnNotifyCallback | None = None
        self._on_archive: OnArchiveCallback | None = None
        # Callback opcional: dado um TopicKey, devolve True se aquele topic
        # tem ask_human pendente (nao resolvido). Usado pro timeout
        # adaptativo do ask_agent — se target esta bloqueado num humano,
        # estende o wait indefinidamente em vez de desistir.
        self._check_target_blocked_on_human: Callable[[TopicKey], Awaitable[bool]] | None = None

    # ---------- registro topic <-> slug ----------

    def register_topic(self, key: TopicKey, conv_id: int | None = None) -> str:
        slug = key.slug()
        self._slug_to_key[slug] = key
        if conv_id is not None:
            self._slug_to_conv_id[slug] = conv_id
        return slug

    def topic_for_slug(self, slug: str) -> TopicKey | None:
        return self._slug_to_key.get(slug)

    def conv_id_for_slug(self, slug: str) -> int | None:
        """D-87: retorna conv_id registrado via register_topic. Usado pelos
        handlers MCP de `__ask_agent`/`__ask_agents_many` pra passar
        parent_conv_id na criacao da nova conv `__ask-from-*`."""
        return self._slug_to_conv_id.get(slug)

    # ---------- callback pra postar no broker ----------

    def set_on_ask(self, cb: OnAskCallback) -> None:
        self._on_ask = cb

    def set_on_ask_agent(self, cb: OnAskAgentCallback) -> None:
        self._on_ask_agent = cb

    def set_on_notify(self, cb: OnNotifyCallback) -> None:
        self._on_notify = cb

    async def notify_human(self, key: TopicKey, message: str) -> None:
        """Posta mensagem simples no topic sem criar pending_ask."""
        if self._on_notify is None:
            raise RuntimeError("notify_human nao configurado neste agente")
        log.info("broker.notify_human", topic=key.slug(), length=len(message))
        await self._on_notify(key, message)

    def set_on_archive(self, cb: OnArchiveCallback) -> None:
        self._on_archive = cb

    async def archive_conversation(self, conv_id: int) -> None:
        if self._on_archive is None:
            raise RuntimeError("archive_conversation nao configurado neste agente")
        log.info("broker.archive_conversation", conv_id=conv_id)
        await self._on_archive(conv_id)

    def set_check_target_blocked_on_human(
        self, cb: Callable[[TopicKey], Awaitable[bool]]
    ) -> None:
        self._check_target_blocked_on_human = cb

    # ---------- estado ----------

    def has_pending(self, key: TopicKey) -> bool:
        fut = self._pending_futures.get(key)
        return fut is not None and not fut.done()

    def pending_question_path(self, key: TopicKey) -> Path:
        return self.pending_dir / f"{key.slug()}.json"

    # ---------- API principal ----------

    async def ask_human(
        self,
        key: TopicKey,
        question: str,
        context: str = "",
    ) -> str:
        """Invocado pelo MCP server quando o claude chama a tool ask_human.

        Sempre bloqueia indefinidamente ate o humano responder. Sem timeout,
        sem fallback — quem nao pode esperar usa `complete_phase(next='halt')`.
        Decisao tomada pos-incidente onde timeout deixava `pending_ask`
        orfao no banco enquanto o agente seguia adiante via fallback,
        confundindo o humano (badge NEEDS YOU permanente em conv que ja
        nao escutava resposta)."""
        asked_iso = datetime.now(tz=timezone.utc).isoformat()
        pending_path = self.pending_question_path(key)
        pending_path.write_text(
            json.dumps(
                {
                    "stream": key.stream,
                    "topic": key.topic,
                    "question": question,
                    "context": context,
                    "asked_at": asked_iso,
                    "asked_at_epoch": time.time(),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        log.info(
            "broker.ask_human",
            topic=key.slug(),
            question_preview=question[:120],
        )

        # Notifica o humano (posta no broker)
        if self._on_ask is not None:
            try:
                await self._on_ask(key, question, context, True)
            except Exception:
                log.exception("broker.on_ask_failed", topic=key.slug())

        # Blocking indefinido: aguarda Future ate alguem (humano via PWA →
        # broker → Dispatcher → broker.resolve()) marcar resultado.
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._pending_futures[key] = fut
        try:
            response = await fut
            log.info("broker.ask_human.resolved", topic=key.slug(), length=len(response))
            return response
        finally:
            self._pending_futures.pop(key, None)
            try:
                pending_path.unlink(missing_ok=True)
            except OSError:
                pass

    def resolve(self, key: TopicKey, response: str) -> bool:
        """Chamado pelo Dispatcher quando chega msg no topic que tem pergunta pendente."""
        fut = self._pending_futures.get(key)
        if fut is None or fut.done():
            return False
        fut.set_result(response)
        log.info("broker.resolved", topic=key.slug(), length=len(response))
        return True

    # ---------- ask_agent (comunicacao entre agentes) ----------

    async def ask_agent_via_callback(
        self,
        *,
        asker_topic: TopicKey | None,
        from_agent: str,
        target_agent: str,
        question: str,
        context: str,
        timeout_minutes: float,
    ) -> str:
        """Helper completo: chama on_ask_agent pra postar a msg (que retorna
        TopicKey criado), registra Future, aguarda resolve/timeout."""
        if self._on_ask_agent is None:
            return "[error] ask_agent is not configured on this agent."
        try:
            target_key, already_resolved = await self._on_ask_agent(
                asker_topic, from_agent, target_agent, question, context
            )
        except Exception as e:
            log.exception("broker.ask_agent.post_failed", target=target_agent)
            return f"[error asking] {e}"

        # D-111 restart-recovery: target ja respondeu enquanto o asker estava
        # down. Retorna direto sem registrar Future nem aguardar.
        if already_resolved is not None:
            log.info(
                "broker.ask_agent.restart_recovered",
                target=target_key.slug(),
                length=len(already_resolved),
            )
            return already_resolved

        log.info(
            "broker.ask_agent",
            target=target_key.slug(),
            timeout_minutes=timeout_minutes,
            question_preview=question[:120],
        )
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._pending_futures[target_key] = fut
        try:
            # Loop com extensao: ao bater no timeout, checamos se o target
            # esta bloqueado num ask_human. Se sim, recomecamos o wait —
            # permanentemente, ate humano responder e target destravar.
            # Se target nao tem ask_human pendente e nao respondeu, timeout real.
            interval = timeout_minutes * 60
            extensions = 0
            while True:
                done, _ = await asyncio.wait({fut}, timeout=interval)
                if fut in done:
                    response = fut.result()
                    log.info(
                        "broker.ask_agent.resolved",
                        target=target_key.slug(),
                        length=len(response),
                        extensions=extensions,
                    )
                    return response
                blocked = False
                if self._check_target_blocked_on_human is not None:
                    try:
                        blocked = await self._check_target_blocked_on_human(target_key)
                    except Exception:
                        log.exception(
                            "broker.ask_agent.check_blocked_failed",
                            target=target_key.slug(),
                        )
                        blocked = False
                if blocked:
                    extensions += 1
                    log.info(
                        "broker.ask_agent.extend_for_human_wait",
                        target=target_key.slug(),
                        extensions=extensions,
                    )
                    continue
                log.warning(
                    "broker.ask_agent.timeout",
                    target=target_key.slug(),
                    extensions=extensions,
                )
                if not fut.done():
                    fut.cancel()
                return f"[timeout] Agente {target_agent} nao respondeu em {timeout_minutes}min."
        finally:
            self._pending_futures.pop(target_key, None)

    async def ask_agents_many(
        self,
        *,
        asker_topic: TopicKey | None,
        from_agent: str,
        asks: list[dict[str, str]],
        timeout_minutes: float,
    ) -> list[dict[str, str]]:
        """Roda N ask_agent_via_callback concorrentemente via asyncio.gather.

        Cada item de `asks` é um dict com `target_agent`, `question`, e
        opcionalmente `context`. Retorna lista na mesma ordem com `response`
        (sucesso) ou `error` (policy/cycle/depth/post/timeout), sem falhar
        o batch inteiro por causa de um item.
        """
        log.info(
            "broker.ask_agents_many.start",
            count=len(asks),
            targets=[a.get("target_agent") for a in asks],
            timeout_minutes=timeout_minutes,
        )

        async def _one(ask: dict[str, str]) -> str:
            return await self.ask_agent_via_callback(
                asker_topic=asker_topic,
                from_agent=from_agent,
                target_agent=ask["target_agent"],
                question=ask["question"],
                context=ask.get("context") or "",
                timeout_minutes=timeout_minutes,
            )

        results = await asyncio.gather(
            *(_one(a) for a in asks), return_exceptions=True
        )
        out: list[dict[str, str]] = []
        for ask, res in zip(asks, results):
            item: dict[str, str] = {
                "target_agent": ask["target_agent"],
                "question": ask["question"],
            }
            if isinstance(res, BaseException):
                item["error"] = f"{type(res).__name__}: {res}"
            else:
                item["response"] = res
            out.append(item)
        log.info(
            "broker.ask_agents_many.done",
            count=len(out),
            errors=sum(1 for o in out if "error" in o),
        )
        return out
