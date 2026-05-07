"""Auto-bootstrap de containers de agente — onboarding magico.

Garante que `agent-framework-agent-<name>-1` esteja rodando, criando-o a
partir do `docker-compose.override.yml` quando necessario. Three cases:

  1. Container ja running -> noop
  2. Container existe mas parado/exited -> start + readiness check
  3. Container nao existe -> reconcile (gera override.yml se faltar) +
     parse override + create via docker SDK + readiness check

MVP: aplica so ao agent-executor (service def previsivel, gerada pelo
proprio reconcile com schema fixo). Nao generalizar prematuramente pra
qualquer agente — tradeoff service-def->SDK kwargs eh fragil.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import docker
import structlog
import yaml


log = structlog.get_logger("agent_bootstrap")

OVERRIDE_PATH = Path("/workspace/docker-compose.override.yml")


def _compose_project() -> str:
    """Project name do compose stack. Default `agent-framework` (framework
    default), override pelo .env da instancia (`COMPOSE_PROJECT_NAME=...`)."""
    import os
    env = _load_dot_env()
    return (
        os.environ.get("COMPOSE_PROJECT_NAME")
        or env.get("COMPOSE_PROJECT_NAME")
        or "agent-framework"
    )


def _container_name(agent: str) -> str:
    return f"{_compose_project()}-agent-{agent}-1"


def _service_name(agent: str) -> str:
    return f"agent-{agent}"


async def _wait_running(container, timeout: float) -> None:
    """Polling: aguarda status=running + claude CLI disponivel pro user node."""
    deadline = asyncio.get_event_loop().time() + timeout
    interval = 0.5
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.to_thread(container.reload)
        if container.status == "running":
            try:
                rc, _ = await asyncio.to_thread(
                    container.exec_run,
                    ["which", "claude"],
                    user="node",
                )
                if rc == 0:
                    return
            except Exception:
                pass
        await asyncio.sleep(interval)
        interval = min(interval * 1.5, 3.0)
    raise TimeoutError(
        f"container {container.name} did not become ready within {timeout}s "
        f"(last status={container.status})"
    )


def _service_to_run_kwargs(service: dict, container_name: str, service_name: str) -> dict:
    """Converte service def do compose pra kwargs do docker SDK containers.run.

    Cobre o subset que reconcile gera pra agent-executor: image, environment,
    volumes (bind mounts), labels, restart, working_dir, command, networks.
    """
    kwargs: dict = {
        "image": service["image"],
        "name": container_name,
        "detach": True,
        "labels": {
            "com.docker.compose.project": _compose_project(),
            "com.docker.compose.service": service_name,
            "com.docker.compose.oneoff": "False",
            **(service.get("labels") or {}),
        },
    }

    # Environment: dict ou lista. Compose suporta ambos.
    env = service.get("environment")
    if isinstance(env, list):
        kwargs["environment"] = env
    elif isinstance(env, dict):
        kwargs["environment"] = {str(k): str(v) for k, v in env.items()}

    # Volumes: lista de strings "host:container[:mode]" -> SDK quer dict.
    # IMPORTANTE: expandir ${VAR:-default} ANTES de split — `:-` dentro do
    # default contem `:` que confunde o split naive. Compose CLI resolve
    # ${...} no host pre-create; sem CLI, web faz a substituicao manual.
    vols: dict = {}
    for v in service.get("volumes") or []:
        if not isinstance(v, str):
            continue
        expanded = _expand_env_vars(v)
        parts = expanded.split(":")
        if len(parts) == 2:
            host, container = parts
            mode = "rw"
        elif len(parts) == 3:
            host, container, mode = parts
        else:
            continue
        vols[host] = {"bind": container, "mode": mode}
    if vols:
        kwargs["volumes"] = vols

    if service.get("working_dir"):
        kwargs["working_dir"] = service["working_dir"]
    if service.get("command"):
        cmd = service["command"]
        kwargs["command"] = cmd if isinstance(cmd, list) else cmd
    if service.get("user"):
        kwargs["user"] = str(service["user"])

    restart = service.get("restart")
    if restart:
        kwargs["restart_policy"] = {"Name": restart}

    # Network: agentes usam a rede default do compose project
    # (<project>_default).
    kwargs["network"] = f"{_compose_project()}_default"

    return kwargs


_ENV_CACHE: dict[str, str] | None = None


def _load_dot_env() -> dict[str, str]:
    global _ENV_CACHE
    if _ENV_CACHE is not None:
        return _ENV_CACHE
    env: dict[str, str] = {}
    p = Path("/workspace/.env")
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    _ENV_CACHE = env
    return env


def _expand_env_vars(s: str) -> str:
    """Expande ${VAR} e ${VAR:-default} usando /workspace/.env. Paths
    relativos (./instance/foo, ../agents) ficam relativos ao manager/ — o
    docker daemon resolve relativo ao compose-project-dir, mas aqui (SDK
    direto) precisa ser absoluto. PROJECT_ROOT_HOST aponta pra manager/."""
    import os
    import re

    project_root_host = os.environ.get("PROJECT_ROOT_HOST", "")
    env = _load_dot_env()
    # HOME do usuario host (passado via compose env HOST_HOME=${HOME}).
    # Compose CLI normalmente resolve ${HOME} no host antes de criar
    # containers; sem CLI, web faz a substituicao manual aqui.
    host_home = os.environ.get("HOST_HOME") or os.environ.get("HOME") or ""

    def repl(m: "re.Match") -> str:
        var = m.group(1)
        default = m.group(2) or ""
        if var == "HOME":
            return host_home or default
        return env.get(var) or default

    # Aceita ${VAR}, ${VAR:-default}, ${VAR-default}. Default pode conter
    # `:` (ex: paths) — usa `[^}]*` que para no fechamento `}`.
    expanded = re.sub(r"\$\{([A-Z_][A-Z0-9_]*)(?::?-([^}]*))?\}", repl, s)

    # Normaliza paths relativos -> absolutos (relativos a manager/)
    if expanded.startswith(("./", "../")) and project_root_host:
        from pathlib import Path as _P
        expanded = str((_P(project_root_host) / expanded).resolve())
    return expanded


async def ensure_agent_running(agent: str, timeout: float = 45.0) -> None:
    """Garantee que agent-framework-agent-<name>-1 esteja running. Idempotent.

    Caller usa antes de docker exec'ar no container (ex: hire generation,
    onboard propose-agents). Levanta RuntimeError/TimeoutError em falha.
    """
    name = _container_name(agent)
    service = _service_name(agent)
    client = await asyncio.to_thread(docker.from_env)

    # Caso 1: ja existe + running
    try:
        container = await asyncio.to_thread(client.containers.get, name)
        await asyncio.to_thread(container.reload)
        if container.status == "running":
            log.debug("ensure.already_running", agent=agent)
            return
        # Caso 2: existe mas parado
        log.info("ensure.starting", agent=agent, prev_status=container.status)
        await asyncio.to_thread(container.start)
        await _wait_running(container, timeout)
        return
    except docker.errors.NotFound:
        pass  # fall through pra caso 3

    # Caso 3: nao existe — gera override se faltar + cria via SDK
    log.info("ensure.bootstrapping", agent=agent)
    if not OVERRIDE_PATH.exists():
        log.info("ensure.running_reconcile", reason="override.yml missing")
        from . import hire as _hire
        await asyncio.to_thread(_hire.run_reconcile)

    if not OVERRIDE_PATH.exists():
        raise RuntimeError(
            f"override.yml still missing after reconcile — cannot bootstrap {agent}"
        )

    override = yaml.safe_load(OVERRIDE_PATH.read_text(encoding="utf-8")) or {}
    services = (override.get("services") or {})
    svc = services.get(service)
    if not svc:
        raise RuntimeError(
            f"service '{service}' not found in override.yml — "
            f"is '{agent}' registered in agents.yaml?"
        )

    kwargs = _service_to_run_kwargs(svc, container_name=name, service_name=service)
    log.info("ensure.creating", agent=agent, image=kwargs.get("image"))

    try:
        container = await asyncio.to_thread(client.containers.run, **kwargs)
    except docker.errors.APIError as e:
        # Race: outro caller pode ter criado entre nosso get(NotFound) e run().
        if "Conflict" in str(e) or "already in use" in str(e):
            container = await asyncio.to_thread(client.containers.get, name)
        else:
            raise

    await _wait_running(container, timeout)
    log.info("ensure.ready", agent=agent)
