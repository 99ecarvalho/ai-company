"""Agent entry point. Runs as `python3 -m ai_company.main` inside the container.

Reads config from env, starts BrokerClient + McpServer + Dispatcher + WorkerPool,
and processes events until it receives SIGTERM/KeyboardInterrupt.
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

    # Shared Postgres pool for the workflow + ask tracker
    import asyncpg
    db_pool = await asyncpg.create_pool(config.broker.database_url, min_size=1, max_size=4)

    # MCP broker + workflow manager + server (all in-process)
    pending_dir = config.agent_home / "pending_questions"
    broker = McpBroker(pending_dir=pending_dir)

    # WorkflowManager is enabled whenever company_dir is writable
    workflow: WorkflowManager | None = None
    try:
        config.workspace_company.mkdir(parents=True, exist_ok=True)
        workflow = WorkflowManager(company_dir=config.workspace_company, db_pool=db_pool)
    except (PermissionError, OSError) as e:
        log.info("agent.workflow_disabled", reason=str(e))

    # Long-term memory (optional — controlled by memory.enabled in agents.yaml)
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

    # HTTP client for the internal broker (messaging + events SSE)
    broker_client = BrokerClient(
        broker_url=config.broker.url,
        token=config.broker.token,
        streams=config.streams,
        database_url=config.broker.database_url,
    )

    # Callback to post the agent's questions to the broker
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
        # Register a pending_ask so the human sees it in Mine + push_notifier fires.
        # Failure is not fatal — the conversation was still posted.
        try:
            await broker_client.create_pending_ask(
                stream=key.stream, topic=key.topic,
                question=question, context=context, blocking=blocking,
            )
        except Exception:
            log.exception("agent.on_ask_register_failed", topic=key.slug())
    broker.set_on_ask(_on_ask)

    # Callback for notify_human (plain post, no pending_ask)
    async def _on_notify(key: TopicKey, message: str) -> None:
        try:
            await broker_client.send_message(key.stream, key.topic, message)
        except Exception:
            log.exception("agent.on_notify_post_failed", topic=key.slug())
    broker.set_on_notify(_on_notify)

    # Callback for archive_conversation (POST /api/conversations/<id>/archive).
    # Errors propagate to the MCP handler, which renders them for the agent — they show up as
    # `archive failed: ...` in the tool result.
    async def _on_archive(conv_id: int) -> None:
        await broker_client.archive_conversation(conv_id)
    broker.set_on_archive(_on_archive)

    # Callback for ask_agent: D-96 flat model (root -> child, at most 1 level).
    # First lookup: is there an active child conv for the (root_conv, target) pair?
    # If so, REUSE it — Claude --resume preserves the child session's context across
    # invocations (persistent child session per pair). If not, create a new conv
    # with topic `__child-<uid8>` (no chain, no regex parsing — the source of
    # truth for the hierarchy is `parent_conv_id` in the schema).
    import uuid as _uuid

    async def _check_policy(from_a: str, target_a: str) -> None:
        """Defense-in-depth: checks messaging.agent_policies. Raises
        ValueError if the policy blocks it. NULL in the columns = no restriction."""
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT can_ask FROM messaging.agent_policies WHERE agent = $1",
                from_a,
            )
            if row and row["can_ask"] is not None and target_a not in row["can_ask"]:
                allowed = ", ".join(row["can_ask"]) or "(none)"
                raise ValueError(
                    f"policy: {from_a} cannot ask {target_a}. "
                    f"Allowed: {allowed}. Consider ask_human."
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
                    f"policy: {target_a} does not accept questions from {from_a}. "
                    f"Consider ask_human."
                )

    async def _on_ask_agent(
        asker_topic: TopicKey | None,
        from_agent: str,
        target_agent: str,
        question: str,
        context: str,
    ) -> tuple[TopicKey, str | None]:
        # Policy check (defense-in-depth, complements the Team filter in the
        # system prompt — Claude may try to ignore context)
        await _check_policy(from_agent, target_agent)

        # D-96: parent_conv_id resolves to the root (asker) conv. The web broker
        # rejects with 409 if the asker is already a child — a child agent does not delegate.
        parent_conv_id = (
            broker.conv_id_for_slug(asker_topic.slug()) if asker_topic else None
        )
        if parent_conv_id is None:
            raise ValueError(
                "ask_agent requires a known parent conv. The current topic has "
                "no registered conv_id — please open an issue."
            )

        # Lookup: active, non-archived child conv for the (parent_conv_id, target) pair.
        # If it exists, reuse the topic — Claude --resume keeps context across invocations.
        # If not, create a new one with `__child-<uid8>`.
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
            # D-111 restart-recovery: the child conv already exists — check whether it already has
            # a pending_ask kind='ask_agent' (resolved or pending). Skip
            # reposting the question so the target doesn't get duplicated context.
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
                    # Ensure the subscription (in case internal_client was
                    # restarted and lost the in-memory subscription).
                    try:
                        await broker_client.subscribe_to_conversation(existing_conv_id)
                    except Exception:
                        log.exception(
                            "agent.ask_agent_subscribe_failed",
                            target=target_key.slug(),
                            conversation_id=existing_conv_id,
                        )
                    return target_key, pa["content"]
                # Unresolved pending: the target has not answered yet. Ensure the
                # subscription and return without reposting or creating a new pending_ask
                # — one already exists, same target, same conv.
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
            # No pending_ask in the conv (pre-D-111 case or conv reused after
            # a previous cycle closed): follow the normal flow (post + create
            # pending_ask).
        else:
            uid = _uuid.uuid4().hex[:8]
            target_key = TopicKey(stream=target_agent, topic=f"__child-{uid}")
            log.info(
                "agent.ask_agent.new_child",
                target=target_key.slug(), parent_conv_id=parent_conv_id,
            )

        # Build the message with a mention of the target to highlight it
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
        # Subscribe ONLY to this conversation (not the target's whole
        # stream) to receive the answer. It used to use subscribe_to_stream,
        # which made the asker receive ALL messages in the target's stream —
        # including questions from OTHER agents, which the asker re-dispatched
        # (double-routing bug in ask_agent).
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
        # D-111: persist a pending_ask kind='ask_agent' for restart-recovery.
        # The current auto-resolve (UPDATE when someone other than the asker posts) already covers it.
        # UI/push filters (Mine, push notifier) use kind='ask_human' so the
        # human is not alerted. Reverts D-46 with an explicit kind.
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
            # does not break the flow: if it fails, ask_agent works as before
            # (in-memory) — it only loses restart-recovery.
        log.info(
            "agent.ask_agent.posted",
            target=target_key.slug(),
            from_agent=from_agent,
            parent_conv_id=parent_conv_id,
        )
        return target_key, None
    broker.set_on_ask_agent(_on_ask_agent)

    # Check for ask_agent's adaptive timeout: true if the target has a pending
    # (unresolved) ask_human. When the broker hits the timeout, it checks
    # this; if true, it extends indefinitely (the human is blocking the loop).
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

    # Recover pending questions from previous runs (crash recovery)
    _recover_pending_questions(broker, broker_client, log)

    # Dispatch components
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
    # Audio transcription: the PWA transcribes locally via /api/transcribe-preview
    # and posts only text; agents no longer receive audio directly.
    dispatcher = Dispatcher(
        pool=pool,
        session_mgr=session_mgr,
        handler=runner,
        broker_client=broker_client,
        broker=broker,
        idle_timeout_sec=config.idle_timeout_sec,
        audio_transcriber=None,
    )
    # D-71: the runner needs the dispatcher to register/unregister the active proc
    # (circular dependency resolved via a post-construction setter).
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
    """Clean up pending questions from previous runs. Does not reopen Futures —
    the agent will have to ask again if needed.
    """
    try:
        for f in broker.pending_dir.glob("*.json"):
            log.warning("agent.pending_question_from_previous_run", file=str(f))
            # We don't rebuild the Future; the previous run's claude is dead.
            # Delete the file so it doesn't linger as a ghost.
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
