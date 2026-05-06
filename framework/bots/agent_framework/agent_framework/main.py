"""Entry point do agente. Roda como `python3 -m agent_framework.main` dentro do container.

Le config via env, sobe BrokerClient + McpServer + Dispatcher + WorkerPool,
processa eventos ate receber SIGTERM/KeyboardInterrupt.
"""
from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path


from .claude_runner import ClaudeRunner
from .config import AgentConfig
from .dispatcher import Dispatcher
from .heartbeat import heartbeat_loop
from .internal_client import InternalClient as BrokerClient
from .internal_client import TopicKey
from .log import get_logger, setup_logging
from .mcp.broker import McpBroker
from .mcp.server import McpServer
from .mcp.workflow import WorkflowManager
from .memory_store import MemoryStore
from .session_manager import SessionManager
from .worker_pool import WorkerPool


async def _run() -> None:
    config = AgentConfig.from_env()
    setup_logging(level=config.log_level, agent_name=config.name)
    log = get_logger("agent.main")
    log.info(
        "agent.starting",
        agent=config.name,
        streams=config.streams,
        pool_size=config.pool_size,
        idle_timeout_sec=config.idle_timeout_sec,
    )

    # Pool de Postgres compartilhado pro workflow + ask tracker
    import asyncpg
    db_pool = await asyncpg.create_pool(config.broker.database_url, min_size=1, max_size=4)

    # MCP broker + workflow manager + server (tudo in-process)
    pending_dir = config.agent_home / "pending_questions"
    broker = McpBroker(pending_dir=pending_dir)

    # WorkflowManager habilitado sempre que company_dir eh gravavel
    workflow: WorkflowManager | None = None
    try:
        config.workspace_company.mkdir(parents=True, exist_ok=True)
        workflow = WorkflowManager(company_dir=config.workspace_company, db_pool=db_pool)
    except (PermissionError, OSError) as e:
        log.info("agent.workflow_disabled", reason=str(e))

    # Memoria de longo prazo (opcional — controlado por memory.enabled em agents.yaml)
    memory: MemoryStore | None = None
    if config.memory_enabled:
        try:
            memory = MemoryStore(agent_name=config.name)
            await memory.start()
            count = await memory.count()
            log.info(
                "agent.memory_enabled",
                count=count,
                auto_inject_limit=config.memory_auto_inject_limit,
            )
        except Exception as e:
            log.info("agent.memory_disabled", reason=str(e))
            memory = None
    else:
        log.info("agent.memory_disabled", reason="config")

    mcp_port = int(os.environ.get("MCP_PORT", "8765"))
    mcp_server = McpServer(
        broker=broker,
        agent_name=config.name,
        workflow=workflow,
        memory=memory,
        host="127.0.0.1",
        port=mcp_port,
        db_pool=db_pool,
    )
    await mcp_server.start()

    # Cliente HTTP do broker interno (messaging + events SSE)
    broker_client = BrokerClient(
        broker_url=config.broker.url,
        token=config.broker.token,
        streams=config.streams,
        database_url=config.broker.database_url,
    )

    # Callback pra postar perguntas do agente no broker
    async def _on_ask(key: TopicKey, question: str, context: str, blocking: bool) -> None:
        tag = "❓ **Question**" if blocking else "ℹ️ **Question (non-blocking)**"
        parts = [f"{tag}", "", question.strip()]
        if context and context.strip():
            parts.extend(["", "_Context:_", context.strip()])
        if blocking:
            parts.extend(["", "_(waiting for your answer in this topic...)_"])
        try:
            await broker_client.send_message(key.stream, key.topic, "\n".join(parts))
        except Exception:
            log.exception("agent.on_ask_post_failed", topic=key.slug())
        # Registra pending_ask pro humano ver em Mine + push_notifier disparar.
        # Falha nao e fatal — conversa ainda foi postada.
        try:
            await broker_client.create_pending_ask(
                stream=key.stream, topic=key.topic,
                question=question, context=context, blocking=blocking,
            )
        except Exception:
            log.exception("agent.on_ask_register_failed", topic=key.slug())
    broker.set_on_ask(_on_ask)

    # Callback pra notify_human (post simples, sem pending_ask)
    async def _on_notify(key: TopicKey, message: str) -> None:
        try:
            await broker_client.send_message(key.stream, key.topic, message)
        except Exception:
            log.exception("agent.on_notify_post_failed", topic=key.slug())
    broker.set_on_notify(_on_notify)

    # Callback pra archive_conversation (POST /api/conversations/<id>/archive).
    # Erro propaga pro handler MCP renderizar pro agente — vai sair como
    # `archive failed: ...` no resultado da tool.
    async def _on_archive(conv_id: int) -> None:
        await broker_client.archive_conversation(conv_id)
    broker.set_on_archive(_on_archive)

    # Callback pra ask_agent: D-96 modelo flat (raiz -> filha, no max 1 nivel).
    # Primeiro lookup: existe child conv ativa pra par (raiz_conv, target)?
    # Se sim, REUSE — Claude --resume preserva contexto da sessao filha entre
    # invocacoes (sessao filha persistente por par). Se nao, cria conv nova
    # com topic `__child-<uid8>` (sem cadeia, sem regex parsing — fonte de
    # verdade da hierarquia eh `parent_conv_id` no schema).
    import uuid as _uuid

    async def _check_policy(from_a: str, target_a: str) -> None:
        """Defense-in-depth: confere messaging.agent_policies. Levanta
        ValueError se policy bloqueia. NULL nas colunas = sem restricao."""
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT can_ask FROM messaging.agent_policies WHERE agent = $1",
                from_a,
            )
            if row and row["can_ask"] is not None and target_a not in row["can_ask"]:
                allowed = ", ".join(row["can_ask"]) or "(ninguem)"
                raise ValueError(
                    f"policy: {from_a} nao pode pedir a {target_a}. "
                    f"Permitidos: {allowed}. Considere ask_human."
                )
            row2 = await conn.fetchrow(
                "SELECT can_be_asked_by FROM messaging.agent_policies WHERE agent = $1",
                target_a,
            )
            if (
                row2
                and row2["can_be_asked_by"] is not None
                and from_a not in row2["can_be_asked_by"]
            ):
                raise ValueError(
                    f"policy: {target_a} nao aceita perguntas de {from_a}. "
                    f"Considere ask_human."
                )

    async def _on_ask_agent(
        asker_topic: TopicKey | None,
        from_agent: str,
        target_agent: str,
        question: str,
        context: str,
    ) -> tuple[TopicKey, str | None]:
        # Policy check (defense-in-depth, complementa o filtro de Equipe no
        # system prompt — Claude pode tentar ignorar contexto)
        await _check_policy(from_agent, target_agent)

        # D-96: parent_conv_id resolve pra conv da raiz (asker). Web broker
        # rejeita 409 se asker ja for filho — agente filha nao delega.
        parent_conv_id = (
            broker.conv_id_for_slug(asker_topic.slug()) if asker_topic else None
        )
        if parent_conv_id is None:
            raise ValueError(
                "ask_agent requer conv pai conhecida. Topic atual nao tem "
                "conv_id registrada — abrir issue."
            )

        # Lookup: child conv ativa nao-arquivada pra par (parent_conv_id, target).
        # Se existe, reusa topic — Claude --resume mantem contexto entre invocacoes.
        # Se nao existe, cria nova com `__child-<uid8>`.
        async with db_pool.acquire() as conn:
            existing = await conn.fetchrow(
                """SELECT c.id AS conv_id, c.topic_name
                     FROM messaging.conversations c
                     JOIN messaging.streams s ON s.id = c.stream_id
                    WHERE c.parent_conv_id = $1
                      AND s.name = $2
                      AND c.archived_at IS NULL
                    ORDER BY c.id DESC
                    LIMIT 1""",
                parent_conv_id, target_agent,
            )
        if existing is not None:
            target_key = TopicKey(stream=target_agent, topic=existing["topic_name"])
            existing_conv_id = int(existing["conv_id"])
            log.info(
                "agent.ask_agent.reuse_child",
                target=target_key.slug(), parent_conv_id=parent_conv_id,
            )
            # D-111 restart-recovery: child conv ja existe — checa se ja tem
            # pending_ask kind='ask_agent' (resolvido ou pendente). Pula
            # repost da question pra nao duplicar contexto pro target.
            async with db_pool.acquire() as conn:
                pa = await conn.fetchrow(
                    """SELECT pa.resolved_at, pa.answer_message_id, pa.kind, m.content
                         FROM messaging.pending_asks pa
                         LEFT JOIN messaging.messages m ON m.id = pa.answer_message_id
                        WHERE pa.conversation_id = $1
                          AND pa.kind = 'ask_agent'""",
                    existing_conv_id,
                )
            if pa is not None:
                if pa["resolved_at"] is not None and pa["content"]:
                    log.info(
                        "agent.ask_agent.recovered_resolved",
                        target=target_key.slug(),
                        answer_message_id=pa["answer_message_id"],
                    )
                    # Garante subscribe (caso o internal_client tenha sido
                    # restartado e perdido a subscription in-memory).
                    try:
                        await broker_client.subscribe_to_conversation(existing_conv_id)
                    except Exception:
                        log.exception(
                            "agent.ask_agent_subscribe_failed",
                            target=target_key.slug(),
                            conversation_id=existing_conv_id,
                        )
                    return target_key, pa["content"]
                # Pending nao-resolvido: target ainda nao respondeu. Garante
                # subscribe e devolve sem repostar nem criar novo pending_ask
                # — ja existe um, mesmo target, mesma conv.
                log.info(
                    "agent.ask_agent.recovered_pending",
                    target=target_key.slug(),
                )
                try:
                    await broker_client.subscribe_to_conversation(existing_conv_id)
                except Exception:
                    log.exception(
                        "agent.ask_agent_subscribe_failed",
                        target=target_key.slug(),
                        conversation_id=existing_conv_id,
                    )
                return target_key, None
            # Sem pending_ask na conv (caso pre-D-111 ou conv reusada apos
            # ciclo anterior fechado): segue o fluxo normal (post + create
            # pending_ask).
        else:
            uid = _uuid.uuid4().hex[:8]
            target_key = TopicKey(stream=target_agent, topic=f"__child-{uid}")
            log.info(
                "agent.ask_agent.new_child",
                target=target_key.slug(), parent_conv_id=parent_conv_id,
            )

        # Montar mensagem com mention do target pra destacar
        parts = [
            f"**Question from `{from_agent}`**",
            "",
            question.strip(),
        ]
        if context and context.strip():
            parts.extend(["", "_Context:_", context.strip()])
        parts.extend(["", f"_Answer here. `{from_agent}` is waiting._"])
        posted = await broker_client.send_message(
            target_key.stream, target_key.topic, "\n".join(parts),
            parent_conv_id=parent_conv_id,
        )
        # Subscrever SOMENTE nesta conversation (nao no stream inteiro do
        # target) pra receber a resposta. Antes usava subscribe_to_stream,
        # que causava o asker a receber TODAS mensagens do stream do target —
        # inclusive perguntas de OUTROS agentes, que o asker re-despachava
        # (double-routing bug em ask_agent).
        conv_id = posted.get("conversation_id") if isinstance(posted, dict) else None
        if conv_id is not None:
            try:
                await broker_client.subscribe_to_conversation(int(conv_id))
            except Exception:
                log.exception(
                    "agent.ask_agent_subscribe_failed",
                    target=target_key.slug(), conversation_id=conv_id,
                )
        else:
            log.warning(
                "agent.ask_agent.no_conv_id",
                target=target_key.slug(), posted=posted,
            )
        # D-111: persiste pending_ask kind='ask_agent' pra restart-recovery.
        # Auto-resolve atual (UPDATE quando alguem nao-asker posta) ja cobre.
        # Filtros em UI/push (Mine, push notifier) usam kind='ask_human' pra
        # nao alertar humano. Reverte D-46 com kind explicito.
        try:
            await broker_client.create_pending_ask(
                stream=target_key.stream,
                topic=target_key.topic,
                question=question,
                context=context or "",
                blocking=True,
                kind="ask_agent",
                target_agent=target_agent,
            )
        except Exception:
            log.exception(
                "agent.ask_agent.pending_ask_failed",
                target=target_key.slug(),
            )
            # nao quebra o fluxo: se falhar, ask_agent funciona como antes
            # (in-memory) — apenas perde restart-recovery.
        log.info(
            "agent.ask_agent.posted",
            target=target_key.slug(),
            from_agent=from_agent,
            parent_conv_id=parent_conv_id,
        )
        return target_key, None
    broker.set_on_ask_agent(_on_ask_agent)

    # Check pro timeout adaptativo do ask_agent: true se target tem ask_human
    # pendente (nao resolvido). Quando o broker bate no timeout, consulta
    # isso; se true, estende indefinidamente (humano esta bloqueando o loop).
    async def _target_blocked_on_human(target_key: TopicKey) -> bool:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT 1 FROM messaging.pending_asks pa
                     JOIN messaging.conversations c ON c.id = pa.conversation_id
                     JOIN messaging.streams s ON s.id = c.stream_id
                    WHERE s.name = $1 AND c.topic_name = $2
                      AND pa.resolved_at IS NULL
                    LIMIT 1""",
                target_key.stream, target_key.topic,
            )
            return row is not None
    broker.set_check_target_blocked_on_human(_target_blocked_on_human)

    # Recovery de perguntas pendentes de runs anteriores (crash recovery)
    _recover_pending_questions(broker, broker_client, log)

    # Componentes de dispatch
    pool = WorkerPool(size=config.pool_size)
    session_mgr = SessionManager(
        agent_home=config.agent_home,
        workspace_repos=config.workspace_repos,
        workspace_company=config.workspace_company,
        sessions_root=config.workspace_sessions,
        db_pool=db_pool,
        main_repo_name=config.main_repo,
    )
    runner = ClaudeRunner(
        session_mgr=session_mgr,
        broker_client=broker_client,
        broker=broker,
        mcp_url_for=mcp_server.url_for,
        allowed_tools=config.allowed_tools,
        telemetry_url=config.telemetry_url,
        agent_name=config.name,
        memory=memory,
        memory_auto_inject_limit=config.memory_auto_inject_limit,
        model=config.model,
        effort=config.effort,
        db_pool=db_pool,
    )
    log.info(
        "agent.runner_config",
        model=config.model or "(default)",
        effort=config.effort or "(default)",
    )
    # Audio transcribe: PWA transcreve localmente via /api/transcribe-preview
    # e posta so texto; agentes nao recebem mais audio diretamente.
    dispatcher = Dispatcher(
        pool=pool,
        session_mgr=session_mgr,
        handler=runner,
        broker_client=broker_client,
        broker=broker,
        idle_timeout_sec=config.idle_timeout_sec,
        audio_transcriber=None,
    )
    # D-71: runner precisa do dispatcher pra register/unregister proc ativo
    # (ciclo circular resolvido via setter pos-construcao).
    runner.bind_dispatcher(dispatcher)

    await broker_client.start()

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        log.info("agent.signal_received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    events_task = asyncio.create_task(_consume_events(broker_client, dispatcher, log))
    heartbeat_task = asyncio.create_task(heartbeat_loop(config.name))

    await stop_event.wait()
    events_task.cancel()
    heartbeat_task.cancel()
    broker_client.stop()
    await mcp_server.stop()
    if memory is not None:
        await memory.close()
    await db_pool.close()
    log.info("agent.shutdown_complete")


def _recover_pending_questions(broker: McpBroker, broker_client: BrokerClient, log) -> None:
    """Limpa perguntas pendentes de runs anteriores. Nao reabre Futures —
    agente vai ter que reperguntar se precisar.
    """
    try:
        for f in broker.pending_dir.glob("*.json"):
            log.warning("agent.pending_question_from_previous_run", file=str(f))
            # Nao reconstroimos o Future; o claude do run anterior morreu.
            # Apagamos o arquivo pra nao ficar fantasma.
            try:
                f.unlink()
            except OSError:
                pass
    except Exception:
        log.exception("agent.recovery_failed")


async def _consume_events(broker_client: BrokerClient, dispatcher: Dispatcher, log) -> None:
    try:
        async for event in broker_client.events():
            try:
                await dispatcher.dispatch(event)
            except Exception:
                log.exception("agent.dispatch_failed", event=event.get("id"))
    except asyncio.CancelledError:
        pass


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
