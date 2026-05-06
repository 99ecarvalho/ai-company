"""Tests do Dispatcher — semaforo turn-level (D-27) + cancel (D-29).

Usa handler mockado em vez de Claude real (independente de CLAUDE_MOCK).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent_framework.dispatcher import Dispatcher
from agent_framework.internal_client import TopicKey
from agent_framework.session_manager import SessionManager
from agent_framework.worker_pool import WorkerPool


class StubHandler:
    """Registra eventos processados; opcionalmente dorme pra simular work."""

    def __init__(self, delay_sec: float = 0.0):
        self.delay_sec = delay_sec
        self.processed: list[tuple[TopicKey, dict]] = []
        self.handle_started = asyncio.Event()

    async def handle(self, event: dict[str, Any], topic_key: TopicKey, workdir: Path) -> None:
        self.handle_started.set()
        if self.delay_sec > 0:
            await asyncio.sleep(self.delay_sec)
        self.processed.append((topic_key, event))


class StubBrokerClient:
    """Registra mensagens postadas (acks de fila, cancel confirms etc)."""

    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    async def send_message(self, stream: str, topic: str, content: str):
        self.sent.append((stream, topic, content))
        return {"id": len(self.sent), "stream": stream, "topic": topic, "content": content}


@pytest.fixture
def session_mgr(tmp_path: Path) -> SessionManager:
    agent_home = tmp_path / "agent"
    agent_home.mkdir()
    (agent_home / "CLAUDE.md").write_text("persona")
    (agent_home / "knowledge").mkdir()
    (tmp_path / "repos").mkdir()
    (tmp_path / "company").mkdir()
    return SessionManager(
        agent_home=agent_home,
        workspace_repos=tmp_path / "repos",
        workspace_company=tmp_path / "company",
    )


def _event(stream: str, topic: str, content: str = "hi", msg_id: int = 1) -> dict:
    return {
        "id": msg_id,
        "type": "stream",
        "sender_id": 42,
        "sender_full_name": "alice",
        "sender_email": "alice@x",
        "sender_is_bot": False,
        "subject": topic,
        "display_recipient": stream,
        "content": content,
        "timestamp": "2026-04-21T00:00:00Z",
    }


async def _wait_for(predicate, timeout: float = 2.0, interval: float = 0.01):
    """Poll curto pra aguardar uma condicao em teste — evita sleeps fixos."""
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - t0 < timeout:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# D-27: semaforo eh turn-level, nao topic-level.
# ---------------------------------------------------------------------------


async def test_slot_liberado_entre_turns(session_mgr: SessionManager):
    """Com pool=1, dois topics diferentes conseguem progredir em sequencia
    sem esperar o idle_timeout do primeiro (antigo comportamento)."""
    pool = WorkerPool(size=1)
    handler = StubHandler(delay_sec=0.05)
    broker_client = StubBrokerClient()
    d = Dispatcher(
        pool=pool, session_mgr=session_mgr, handler=handler,
        broker_client=broker_client, idle_timeout_sec=60,
    )

    await d.dispatch(_event("s", "topic-a", msg_id=1))
    await d.dispatch(_event("s", "topic-b", msg_id=2))

    # Ambos processam antes de qualquer idle timeout.
    ok = await _wait_for(lambda: len(handler.processed) == 2, timeout=2.0)
    assert ok, f"esperava 2 processados, tem {len(handler.processed)}"

    # Slot foi liberado entre A e B — in_use volta a 0 logo apos.
    ok = await _wait_for(lambda: pool.in_use == 0, timeout=1.0)
    assert ok
    # Topic loops de A e B ficam "vivos" (dormindo em queue.get) mas nao
    # segurando slot — exatamente o ponto da refatoracao.


async def test_dois_topics_concorrentes_com_pool_2(session_mgr: SessionManager):
    """Com pool=2, dois topics rodam o handler em paralelo."""
    pool = WorkerPool(size=2)
    handler = StubHandler(delay_sec=0.1)
    d = Dispatcher(
        pool=pool, session_mgr=session_mgr, handler=handler,
        broker_client=StubBrokerClient(), idle_timeout_sec=60,
    )
    await d.dispatch(_event("s", "a"))
    await d.dispatch(_event("s", "b"))

    # Ambos devem estar em handle simultaneamente em algum ponto.
    # Detecta via pool.in_use chegando a 2.
    ok = await _wait_for(lambda: pool.in_use == 2, timeout=1.0)
    assert ok, "esperava 2 handlers rodando em paralelo"


async def test_ack_fila_posta_quando_pool_cheio(session_mgr: SessionManager):
    """Se pool cheio no inicio de um turn, posta ack ⏳ Aguardando slot."""
    pool = WorkerPool(size=1)
    slow = StubHandler(delay_sec=0.3)
    broker_client = StubBrokerClient()
    d = Dispatcher(
        pool=pool, session_mgr=session_mgr, handler=slow,
        broker_client=broker_client, idle_timeout_sec=60,
    )
    await d.dispatch(_event("s", "first"))
    # Garante que primeiro turn esta com slot
    await slow.handle_started.wait()
    # Segundo topic: pool cheio → deve postar ack
    await d.dispatch(_event("s", "second"))
    ok = await _wait_for(
        lambda: any("Aguardando slot" in c for (_, _, c) in broker_client.sent),
        timeout=1.0,
    )
    assert ok, f"esperava ack de fila; recebido: {broker_client.sent}"


async def test_topicos_internos_nao_postam_ack_fila(session_mgr: SessionManager):
    """Tópicos `__ask-...` não poluem humano com ack de fila."""
    pool = WorkerPool(size=1)
    slow = StubHandler(delay_sec=0.3)
    broker_client = StubBrokerClient()
    d = Dispatcher(
        pool=pool, session_mgr=session_mgr, handler=slow,
        broker_client=broker_client, idle_timeout_sec=60,
    )
    await d.dispatch(_event("s", "first"))
    await slow.handle_started.wait()
    await d.dispatch(_event("s", "__ask-from-X-123"))
    # Deixa o loop rodar um pouco
    await asyncio.sleep(0.05)
    msgs = [c for (_, _, c) in broker_client.sent]
    assert not any("Aguardando slot" in m for m in msgs), \
        f"topicos internos nao devem postar ack: {msgs}"


# ---------------------------------------------------------------------------
# D-29: cancel_topic via evento de controle.
# ---------------------------------------------------------------------------


async def test_cancel_sem_task_retorna_info(session_mgr: SessionManager):
    pool = WorkerPool(size=1)
    broker_client = StubBrokerClient()
    d = Dispatcher(
        pool=pool, session_mgr=session_mgr, handler=StubHandler(),
        broker_client=broker_client, idle_timeout_sec=60,
    )
    await d.dispatch({
        "_ctrl": True, "ctrl_type": "cancel_topic",
        "display_recipient": "s", "subject": "nada",
    })
    msgs = [c for (_, _, c) in broker_client.sent]
    assert any("Nada pra cancelar" in m for m in msgs), msgs


async def test_cancel_silent_nao_posta_nada(session_mgr: SessionManager):
    """Cancel disparado por archive/delete (silent=True) nao posta msg
    de confirmacao na conv — evita poluir tab Closed."""
    pool = WorkerPool(size=1)
    broker_client = StubBrokerClient()
    d = Dispatcher(
        pool=pool, session_mgr=session_mgr, handler=StubHandler(),
        broker_client=broker_client, idle_timeout_sec=60,
    )
    await d.dispatch({
        "_ctrl": True, "ctrl_type": "cancel_topic",
        "display_recipient": "s", "subject": "nada", "silent": True,
    })
    msgs = [c for (_, _, c) in broker_client.sent]
    assert not any("Nada pra cancelar" in m for m in msgs), msgs
    assert not any("Cancelado pelo usuario" in m for m in msgs), msgs


async def test_cancel_antes_de_rodar_dreina_e_avisa(session_mgr: SessionManager):
    """Topic pendurado em pool.acquire (ocupado por outro) eh cancelavel."""
    pool = WorkerPool(size=1)
    slow = StubHandler(delay_sec=1.0)  # segura slot
    broker_client = StubBrokerClient()
    d = Dispatcher(
        pool=pool, session_mgr=session_mgr, handler=slow,
        broker_client=broker_client, idle_timeout_sec=60,
    )
    await d.dispatch(_event("s", "first"))
    await slow.handle_started.wait()
    # Segundo topic entra — vai ficar esperando pool.acquire
    await d.dispatch(_event("s", "second"))
    # Aguarda ack de fila pra confirmar que second ja entrou em _run_turn
    await _wait_for(
        lambda: any("Aguardando slot" in c for (_, _, c) in broker_client.sent),
        timeout=1.0,
    )
    # Cancela
    await d.dispatch({
        "_ctrl": True, "ctrl_type": "cancel_topic",
        "display_recipient": "s", "subject": "second",
    })
    ok = await _wait_for(
        lambda: any("Cancelado pelo usuario" in c for (_, _, c) in broker_client.sent),
        timeout=1.0,
    )
    assert ok, f"esperava confirmacao de cancel; msgs: {broker_client.sent}"


async def test_cancel_durante_run_sem_proc_registrado_retorna_too_late(session_mgr: SessionManager):
    """Se handler esta rodando mas nao registrou proc (pre-D-71 ou pre-spawn),
    cancel retorna `too_late`."""
    pool = WorkerPool(size=1)
    # Handler "cola" ate liberarmos explicitamente — mais confiavel que sleep.
    release = asyncio.Event()

    class StickyHandler(StubHandler):
        async def handle(self, event, topic_key, workdir):
            self.handle_started.set()
            await release.wait()
            self.processed.append((topic_key, event))

    handler = StickyHandler()
    broker_client = StubBrokerClient()
    d = Dispatcher(
        pool=pool, session_mgr=session_mgr, handler=handler,
        broker_client=broker_client, idle_timeout_sec=60,
    )
    await d.dispatch(_event("s", "t"))
    await handler.handle_started.wait()
    await d.dispatch({
        "_ctrl": True, "ctrl_type": "cancel_topic",
        "display_recipient": "s", "subject": "t",
    })
    ok = await _wait_for(
        lambda: any("Tarde demais" in c for (_, _, c) in broker_client.sent),
        timeout=1.0,
    )
    assert ok, f"esperava too_late; msgs: {broker_client.sent}"
    # Libera handler pra cleanup limpo
    release.set()


# ---------------------------------------------------------------------------
# D-71: cancel com proc registrado manda SIGTERM + SIGKILL fallback.
# ---------------------------------------------------------------------------


class _FakeProc:
    """Subset minimo de asyncio.subprocess.Process pra testar kill path.

    Captura terminate/kill + permite esperar wait() ate controle manual.
    """
    def __init__(self, ignore_terminate: bool = False):
        self.pid = 12345
        self.terminate_called = False
        self.kill_called = False
        self._exit_event = asyncio.Event()
        self._ignore_terminate = ignore_terminate

    def terminate(self):
        self.terminate_called = True
        if not self._ignore_terminate:
            self._exit_event.set()

    def kill(self):
        self.kill_called = True
        self._exit_event.set()

    async def wait(self) -> int:
        await self._exit_event.wait()
        return 0


async def test_cancel_durante_run_com_proc_registrado_manda_sigterm(session_mgr: SessionManager):
    """D-71: handler registrou proc → cancel manda SIGTERM via Process.terminate()."""
    pool = WorkerPool(size=1)
    release = asyncio.Event()
    fake_proc = _FakeProc()

    class HandlerComProc(StubHandler):
        def __init__(self, disp):
            super().__init__()
            self._disp = disp

        async def handle(self, event, topic_key, workdir):
            self.handle_started.set()
            self._disp.handler_register_proc(topic_key, fake_proc)
            try:
                await release.wait()
            finally:
                self._disp.handler_unregister_proc(topic_key)
            self.processed.append((topic_key, event))

    broker_client = StubBrokerClient()
    d = Dispatcher(
        pool=pool, session_mgr=session_mgr, handler=None,  # seta depois
        broker_client=broker_client, idle_timeout_sec=60,
    )
    d.handler = HandlerComProc(d)
    await d.dispatch(_event("s", "t"))
    await d.handler.handle_started.wait()
    # Cancel: dispatcher deve encontrar o proc registrado e chamar terminate().
    await d.dispatch({
        "_ctrl": True, "ctrl_type": "cancel_topic",
        "display_recipient": "s", "subject": "t",
    })
    ok = await _wait_for(lambda: fake_proc.terminate_called, timeout=1.0)
    assert ok, "esperava fake_proc.terminate() chamado"
    # Mensagem de confirmacao tem SIGTERM no texto pra humano entender.
    ok = await _wait_for(
        lambda: any("SIGTERM" in c for (_, _, c) in broker_client.sent),
        timeout=1.0,
    )
    assert ok, f"esperava confirmacao com SIGTERM; msgs: {broker_client.sent}"
    # Libera pra cleanup.
    release.set()


async def test_cancel_sigkill_fallback_se_sigterm_ignorado(session_mgr: SessionManager):
    """D-71: se SIGTERM nao derruba em 3s, SIGKILL entra."""
    pool = WorkerPool(size=1)
    release = asyncio.Event()
    fake_proc = _FakeProc(ignore_terminate=True)

    class HandlerComProc(StubHandler):
        def __init__(self, disp):
            super().__init__()
            self._disp = disp

        async def handle(self, event, topic_key, workdir):
            self.handle_started.set()
            self._disp.handler_register_proc(topic_key, fake_proc)
            try:
                await release.wait()
            finally:
                self._disp.handler_unregister_proc(topic_key)
            self.processed.append((topic_key, event))

    broker_client = StubBrokerClient()
    d = Dispatcher(
        pool=pool, session_mgr=session_mgr, handler=None,
        broker_client=broker_client, idle_timeout_sec=60,
    )
    d.handler = HandlerComProc(d)
    await d.dispatch(_event("s", "t"))
    await d.handler.handle_started.wait()
    await d.dispatch({
        "_ctrl": True, "ctrl_type": "cancel_topic",
        "display_recipient": "s", "subject": "t",
    })
    # Trigger fallback com timeout curto (evita waitar 3s reais no teste).
    asyncio.create_task(d._sigkill_fallback(
        TopicKey(stream="s", topic="t"), fake_proc, timeout_sec=0.1,
    ))
    ok = await _wait_for(lambda: fake_proc.kill_called, timeout=1.0)
    assert ok, "esperava fake_proc.kill() chamado apos timeout do SIGTERM"
    release.set()


async def test_handler_register_unregister_isola_topics(session_mgr: SessionManager):
    """Dict _topic_procs indexado por slug — topics diferentes nao colidem."""
    d = Dispatcher(
        pool=WorkerPool(size=1), session_mgr=session_mgr,
        handler=StubHandler(), idle_timeout_sec=60,
    )
    k1 = TopicKey(stream="s", topic="a")
    k2 = TopicKey(stream="s", topic="b")
    p1 = _FakeProc()
    p2 = _FakeProc()
    d.handler_register_proc(k1, p1)
    d.handler_register_proc(k2, p2)
    assert d._topic_procs[k1.slug()] is p1
    assert d._topic_procs[k2.slug()] is p2
    d.handler_unregister_proc(k1)
    assert k1.slug() not in d._topic_procs
    assert d._topic_procs[k2.slug()] is p2
    # Idempotente — unregister de slug ausente nao explode.
    d.handler_unregister_proc(k1)
