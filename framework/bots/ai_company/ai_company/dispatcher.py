"""Dispatcher: 1 topic = 1 worker exclusivo, 1 queue por topic.

Garante:
  - Mensagens consecutivas no mesmo topic vao pro mesmo worker em ordem
  - `pool_size` eh TURN-LEVEL (D-27): limita invocacoes Claude concorrentes,
    nao topics "vivos". Slot soh eh segurado durante `handler.handle()`.
  - Topic loop persiste entre turns pra manter workdir e session_id do topic
    "quentes" — libera soh apos `idle_timeout_sec` sem msgs.
  - Se pool cheio no inicio de um turn, posta ack no chat antes de aguardar.
  - Contencao de pool > CONTENTION_LOG_THRESHOLD_SEC vira log `pool.contention`.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Protocol


from .log import get_logger
from .mcp.broker import McpBroker
from .session_manager import SessionManager
from .worker_pool import WorkerPool
from .internal_client import TopicKey, InternalClient as BrokerClient

log = get_logger(__name__)

MENTION_RE = re.compile(r"@\*\*[^*]+\*\*\s*")

# Log `pool.contention` quando um turn espera mais que isso pra adquirir o slot.
CONTENTION_LOG_THRESHOLD_SEC = 2.0


class EventHandler(Protocol):
    """Protocolo: algo que sabe processar um evento num cwd dado.
    Implementado por ClaudeRunner ou mocks nos testes.
    """
    async def handle(self, event: dict[str, Any], topic_key: TopicKey, workdir) -> None: ...


class Dispatcher:
    def __init__(
        self,
        pool: WorkerPool,
        session_mgr: SessionManager,
        handler: EventHandler,
        broker_client: BrokerClient | None = None,
        broker: McpBroker | None = None,
        idle_timeout_sec: int = 900,
        audio_transcriber=None,  # mantido por compat; no-op (PWA transcreve)
    ):
        self.pool = pool
        self.session_mgr = session_mgr
        self.handler = handler
        self.broker_client = broker_client
        self.broker = broker
        self.idle_timeout_sec = idle_timeout_sec
        self._queues: dict[TopicKey, asyncio.Queue[dict]] = {}
        self._tasks: dict[TopicKey, asyncio.Task] = {}
        self._running_handler: set[TopicKey] = set()
        # D-71: handler ativo guarda o proc do Claude CLI aqui pra permitir
        # cancel em pleno turn (SIGTERM + SIGKILL fallback). Slug → Process.
        # Life-time == duracao da chamada ao subprocess no claude_runner;
        # register/unregister vem do proprio runner via handler_register_proc.
        self._topic_procs: dict[str, asyncio.subprocess.Process] = {}
        # D-72: quando cancel_topic eh invocado pelo humano com SIGTERM bem
        # sucedido, marcamos o slug aqui. Claude runner consulta via
        # `consume_user_cancel` no retry loop — se setado, trata como
        # nao-retriable (evita auto-resume de rc=143). Set limpa no consume.
        self._user_cancelled: set[str] = set()
        self._lock = asyncio.Lock()

    def consume_user_cancel(self, key: TopicKey) -> bool:
        """Check-and-clear: True se houve cancel do humano nesse topic
        desde a ultima checagem. Usado pelo runner pra nao retentar apos
        SIGTERM vindo de cancel_topic (D-72)."""
        slug = key.slug()
        if slug in self._user_cancelled:
            self._user_cancelled.discard(slug)
            return True
        return False

    def handler_register_proc(self, key: TopicKey, proc: asyncio.subprocess.Process) -> None:
        """Chamado pelo handler (ClaudeRunner) ao spawnar o CLI. D-71.

        Mantem ref do process por topic pra permitir SIGTERM em cancel_topic
        quando o handler ja esta rodando (pre-D-71 respondia "tarde demais").
        """
        self._topic_procs[key.slug()] = proc

    def handler_unregister_proc(self, key: TopicKey) -> None:
        """Chamado pelo handler ao encerrar o CLI (finally apos proc.wait).

        Idempotente — pop sem KeyError pra casos de race com cancel.
        """
        self._topic_procs.pop(key.slug(), None)

    async def _maybe_transcribe_audio(self, event: dict[str, Any], key: TopicKey) -> None:
        return  # no-op — PWA transcreve localmente via /api/transcribe-preview

    async def dispatch(self, event: dict[str, Any]) -> None:
        # Eventos de controle (cancel, etc) vem do canal `agent_ctrl` via
        # InternalClient e nao sao mensagens normais — roteamento separado.
        if event.get("_ctrl"):
            await self._handle_ctrl(event)
            return

        key = TopicKey(stream=event["display_recipient"], topic=event["subject"])

        # Se tem audio anexado, transcreve e enriquece content ANTES de qualquer
        # roteamento. Util tb pra ask_human: humano pode responder via voz.
        await self._maybe_transcribe_audio(event, key)

        # Se tem ask_human OU ask_agent pendente neste topic, a msg eh a resposta.
        # Resolve o Future direto (nao enfileira pro worker). Echo do proprio
        # agente ja foi filtrado upstream em internal_client._on_notify via
        # `sender_id == self.user_id` — qualquer msg que chega aqui e de outro
        # sender. Pos-D-96 nao ha mais ask_human em conv filha (gateado no
        # MCP), entao filhas so postam resposta final do Claude. Filtros
        # emoji `:question:`/`:loudspeaker:`/`:hourglass:` removidos.
        if self.broker is not None and self.broker.has_pending(key):
            raw_content = str(event.get("content") or "")
            content = MENTION_RE.sub("", raw_content).strip()
            if content and self.broker.resolve(key, content):
                log.info("dispatcher.routed_to_broker", topic=key.slug(), length=len(content))
                return

        # D-75: evento cuja stream NAO pertence a este agente so deve
        # servir pra destravar ask_agent/ask_human (path acima). Se chegou
        # aqui, significa que a conv e filha (via subscribe_to_conversation)
        # e a msg e irrelevante pro loop do agente — descartar pra nao
        # criar topic_loop local em topic alheio (causava loops entre pais
        # e filhos, o pai processava msgs da conv filha como proprias).
        # `getattr` com default lista vazia: se broker_client nao expoe
        # owned_streams (stubs de teste), skip o filtro (backward compat).
        owned = getattr(self.broker_client, "owned_streams", None)
        if owned is not None and key.stream not in owned:
            log.info(
                "dispatcher.skip_foreign_stream",
                topic=key.slug(),
                own_streams=owned,
            )
            return

        async with self._lock:
            if key not in self._queues:
                self._queues[key] = asyncio.Queue()
                self._tasks[key] = asyncio.create_task(
                    self._topic_loop(key), name=f"topic-{key.slug()}"
                )
            await self._queues[key].put(event)
        log.debug("dispatcher.queued", topic=key.slug(), pending=self._queues[key].qsize())

    async def _topic_loop(self, key: TopicKey) -> None:
        """Loop do topic: setup workdir, consome queue ate idle timeout.

        NAO segura slot de pool — cada turn adquire o seu em `_run_turn`.
        """
        workdir = self.session_mgr.setup(key)
        log.info("topic.started", topic=key.slug(), workdir=str(workdir))
        queue = self._queues[key]
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=self.idle_timeout_sec)
                except asyncio.TimeoutError:
                    log.info("topic.idle_timeout", topic=key.slug())
                    break
                await self._run_turn(event, key, workdir)
        except asyncio.CancelledError:
            log.info("topic.cancelled", topic=key.slug())
            # Nao re-levantar — queremos sair limpo via finally
        finally:
            async with self._lock:
                # Se chegaram eventos novos durante a saida, re-dispara.
                # Nao aplica quando fomos cancelados (queue ja drenada pelo
                # `_handle_ctrl`).
                if key in self._queues and not self._queues[key].empty():
                    leftover = self._queues[key]
                    self._queues[key] = asyncio.Queue()
                    self._tasks[key] = asyncio.create_task(
                        self._topic_loop(key), name=f"topic-{key.slug()}"
                    )
                    # re-enqueue leftovers na nova queue
                    while not leftover.empty():
                        await self._queues[key].put(leftover.get_nowait())
                else:
                    self._queues.pop(key, None)
                    self._tasks.pop(key, None)
            log.info("topic.ended", topic=key.slug())

    async def _run_turn(self, event: dict[str, Any], key: TopicKey, workdir) -> None:
        """Adquire slot do pool, processa 1 evento, libera.

        Posta ack "aguardando slot" se pool cheio. Loga `pool.contention`
        quando espera ultrapassa `CONTENTION_LOG_THRESHOLD_SEC`.
        """
        label = f"topic={key.slug()}"
        # Se pool cheio antes do acquire, avisa no chat.
        # Filtro: nao posta ack pra topics internos (`__ask-...`) — poluiria.
        if self.pool.free == 0 and self.broker_client is not None and not key.topic.startswith("__"):
            try:
                await self.broker_client.send_message(
                    key.stream, key.topic,
                    f"⏳ Aguardando slot — {self.pool.in_use}/{self.pool.size} em uso. "
                    f"Processarei este turno assim que liberar.",
                )
            except Exception:
                log.exception("dispatcher.slot_wait_notify_failed")

        t0 = asyncio.get_event_loop().time()
        async with self.pool.acquire(label=label):
            waited = asyncio.get_event_loop().time() - t0
            if waited > CONTENTION_LOG_THRESHOLD_SEC:
                log.info(
                    "pool.contention",
                    topic=key.slug(), waited_sec=round(waited, 2),
                    pool_size=self.pool.size,
                )
            self.session_mgr.touch(key)
            self._running_handler.add(key)
            try:
                await self.handler.handle(event, key, workdir)
            except Exception:
                log.exception("topic.handler_error", topic=key.slug())
            finally:
                self._running_handler.discard(key)
                # D-78: avanca cursor DEPOIS do turno, nao no enqueue.
                # Garante que msgs enfileiradas + nao processadas (pool cheio
                # + container cai) sao replayadas no proximo start. Chama mesmo
                # se handler.handle crashou — consideramos a msg "tentada",
                # senao fica em loop eterno de replay (claude_runner ja faz
                # MAX_ATTEMPTS retries internos). Fire-and-forget; broker usa
                # GREATEST pra nao regredir cursor em chamadas concorrentes.
                if self.broker_client is not None:
                    stream = event.get("display_recipient")
                    msg_id = event.get("id")
                    if stream and isinstance(msg_id, int):
                        mark = getattr(self.broker_client, "mark_processed", None)
                        if callable(mark):
                            mark(stream, msg_id)

    async def _handle_ctrl(self, event: dict[str, Any]) -> None:
        """Processa eventos de controle (ex.: cancel_topic) vindos do canal
        Postgres `agent_ctrl` via InternalClient."""
        ctrl_type = event.get("ctrl_type")
        stream = event.get("display_recipient")
        topic = event.get("subject")
        if not stream or not topic:
            log.warning("dispatcher.ctrl_missing_target", event=event)
            return
        key = TopicKey(stream=stream, topic=topic)

        if ctrl_type == "cancel_topic":
            await self._cancel_topic(key, silent=bool(event.get("silent", False)))
            return
        log.warning("dispatcher.unknown_ctrl_type", type=ctrl_type, topic=key.slug())

    async def _cancel_topic(self, key: TopicKey, silent: bool = False) -> None:
        """Cancela turn — drena queue, mata proc em execucao, ou reporta no-op.

        Fluxo (D-71: branch kill_proc estende pre-existente):
          - Sem task ativa: nada pra cancelar (mensagem info no chat).
          - Handler ja rodando E proc registrado: SIGTERM no CLI + fallback
            SIGKILL apos KILL_TIMEOUT_SEC. Runner volta com error_subtype
            e emite live_event `run_end` naturalmente.
          - Handler ja rodando mas SEM proc registrado (pre-spawn ou
            pos-exit): tarde demais.
          - Task pendurada em queue.get/pool.acquire: drena queue + cancela
            task (mensagem confirmacao no chat).

        `silent=True` suprime as mensagens de confirmacao no chat. Usado
        quando o cancel foi disparado por archive/delete da conv: a conv
        ja sumiu da tab Active do humano, postar feedback so polui a tab
        Closed.
        """
        too_late = False
        no_task = False
        killed_proc: asyncio.subprocess.Process | None = None
        async with self._lock:
            task = self._tasks.get(key)
            queue = self._queues.get(key)
            if task is None or task.done():
                no_task = True
            elif key in self._running_handler:
                killed_proc = self._topic_procs.get(key.slug())
                if killed_proc is None:
                    too_late = True
            else:
                # Safe pra cancelar: drena queue e cancela task.
                if queue is not None:
                    drained = 0
                    try:
                        while True:
                            queue.get_nowait()
                            drained += 1
                    except asyncio.QueueEmpty:
                        pass
                    log.info("dispatcher.cancel.queue_drained", topic=key.slug(), count=drained)
                task.cancel()

        # SIGTERM fora do lock — o proc.wait() dele vai rodar em paralelo
        # no loop do runner (que segura o lock do pool, nao do dispatcher).
        if killed_proc is not None:
            try:
                killed_proc.terminate()
                log.info(
                    "dispatcher.cancel.sigterm",
                    topic=key.slug(), pid=killed_proc.pid,
                )
            except ProcessLookupError:
                # Proc ja terminou entre o check e o terminate — race normal.
                log.info("dispatcher.cancel.noop_raced", topic=key.slug())
                killed_proc = None
            except Exception:
                log.exception("dispatcher.cancel.sigterm_failed", topic=key.slug())
            else:
                # D-72: marca o topic como cancelado pelo humano pra runner
                # nao tratar o rc=143 como retriable erro externo e auto-resumir.
                self._user_cancelled.add(key.slug())
                # Agenda SIGKILL como fallback sem bloquear este handler.
                asyncio.create_task(self._sigkill_fallback(key, killed_proc))

        # Notificacoes fora do lock pra nao segurar estado.
        if self.broker_client is None:
            return
        if silent:
            # Cancel disparado por archive/delete: estado interno ja foi
            # tratado, nao postamos nada na conv (que esta sumindo).
            log.info(
                "dispatcher.cancel.silent",
                topic=key.slug(),
                outcome=(
                    "no_task" if no_task
                    else "too_late" if too_late
                    else "kill_requested" if killed_proc is not None
                    else "done"
                ),
            )
            return
        try:
            if no_task:
                await self.broker_client.send_message(
                    key.stream, key.topic,
                    "ℹ️ Nothing to cancel — no pending turn.",
                )
                log.info("dispatcher.cancel.no_task", topic=key.slug())
            elif too_late:
                await self.broker_client.send_message(
                    key.stream, key.topic,
                    "⚠️ Too late — Claude is already finishing this turn.",
                )
                log.info("dispatcher.cancel.too_late", topic=key.slug())
            elif killed_proc is not None:
                await self.broker_client.send_message(
                    key.stream, key.topic,
                    "🚫 Cancelled by user (SIGTERM sent to the Claude CLI).",
                )
                log.info("dispatcher.cancel.kill_requested", topic=key.slug())
            else:
                await self.broker_client.send_message(
                    key.stream, key.topic,
                    "🚫 Cancelled by user.",
                )
                log.info("dispatcher.cancel.done", topic=key.slug())
        except Exception:
            log.exception("dispatcher.cancel.notify_failed", topic=key.slug())

    async def _sigkill_fallback(
        self, key: TopicKey, proc: asyncio.subprocess.Process,
        timeout_sec: float = 3.0,
    ) -> None:
        """Se o SIGTERM nao derrubou o proc em `timeout_sec`, manda SIGKILL.

        Claude CLI geralmente respeita SIGTERM (limpa stream-json + emite
        `result`), mas sob certos estados (tool_use pendurado em network)
        pode ficar pendurado. SIGKILL garante liberacao do slot do pool.
        """
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_sec)
            return  # terminou limpo
        except asyncio.TimeoutError:
            pass
        try:
            proc.kill()
            log.warning(
                "dispatcher.cancel.sigkill",
                topic=key.slug(), pid=proc.pid, waited_sec=timeout_sec,
            )
        except ProcessLookupError:
            pass
        except Exception:
            log.exception("dispatcher.cancel.sigkill_failed", topic=key.slug())
