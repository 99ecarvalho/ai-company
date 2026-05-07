"""Hire Assistant — gera CLAUDE.md + agents.yaml entry pra um agente novo,
chamando o Claude CLI via `docker exec` em um container existente (reusa
o auth Claude ja montado no agent-debug).
"""
from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import docker
import structlog
import yaml


log = structlog.get_logger("hire")

import os

# Reusa auth Claude rodando `claude -p` dentro de um container de agente existente
# (que ja tem CLI Claude + creds montadas). Default = executor; override via
# HIRE_AGENT_CONTAINER se sua instance usa outro nome.
EXEC_CONTAINER = os.environ.get(
    "HIRE_AGENT_CONTAINER", "agent-framework-agent-executor-1"
)
AGENTS_YAML = Path("/workspace/agents/agents.yaml")
AGENTS_DIR = Path("/workspace/agents")
PROJECT_ROOT = Path("/workspace")                      # dentro do web container
PROJECT_ROOT_HOST = os.environ.get("PROJECT_ROOT_HOST", "/workspace")  # path no daemon (host)

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")

VALID_MOUNTS = {"company", "orchestrator", "repos"}


# Prompt pra o Claude gerar CLAUDE.md + yaml entry (como objeto JSON — serializamos pra YAML no backend)
GENERATION_PROMPT = """\
Voce esta sendo contratado pra ajudar o usuario a montar um "funcionario" (agente Claude Code)
pra empresa virtual dele. Com base nas respostas abaixo, gere uma configuracao completa.

=== DADOS DO NOVO AGENTE ===

{data_block}

=== FORMATO DA RESPOSTA ===

Sua resposta DEVE ser um unico objeto JSON valido, sem markdown fences, com exatamente:

{{
  "entry": {{
    "name": "<slug>",
    "display_name": "<Nome Exibicao>",
    "description": "<uma linha>",
    "streams": ["<slug>"],
    "pool_size": <int>,
    "idle_timeout_sec": <int>,
    "write_access": ["<mount>", ...],
    "read_access": ["<mount>", ...],
    "memory": true,
    "model": "<opcional: sonnet|opus|haiku ou id completo>",
    "effort": "<opcional: low|medium|high|xhigh|max>",
    "allowed_tools": ["<tool>", ...]
  }},
  "claude_md": "<string com markdown completo do CLAUDE.md>"
}}

=== REGRAS ===

entry:
- name: exatamente o slug passado em "name (slug)" nos dados acima.
- pool_size: 2 default; 1 se papel exige serializacao; 3-5 se throughput.
- idle_timeout_sec: 900 default; 1800-3600 pra tarefas longas (pesquisa, implementacao).
- write_access / read_access: listas. Opcoes: "company", "orchestrator", "repos". NAO repetir mesmo mount nos dois.
- memory: true (default).
- model: OMITA por default. Inclua so se o papel exige capacidade especifica
  ("opus" pra raciocinio pesado; "haiku" pra tarefas simples/rapidas/baratas).
- effort: OMITA por default. Use "high" pra papeis que exigem raciocinio profundo
  (planejador, revisor); "low" pra tarefas mecanicas/repetitivas (captura simples).
- allowed_tools: inclua SEMPRE "mcp__agent_framework__ask_human" e as 3 de memoria:
  "mcp__agent_framework__memory_save", "mcp__agent_framework__memory_recall", "mcp__agent_framework__memory_list".
  Se participa de workflow multi-agente: inclua "mcp__agent_framework__complete_phase".
  Se precisa ler/editar arquivos: "Read", "Write", "Edit", "Glob", "Grep".
  Se precisa rodar comandos: "Bash".
  Se precisa buscar na web: "WebFetch", "WebSearch".
  Considere "mcp__agent_framework__ask_agent" se o papel provavelmente precisa consultar outro agente.

claude_md:
- Portugues brasileiro.
- Primeira linha: "# <display_name>".
- Inclua as secoes:
  * ## Persona / politica (voz, principios do papel, vocabulario)
  * ## Formato de resposta esperado (em que diretorio escreve, estrutura dos arquivos)
  * ## Tools disponiveis (auto-aprovadas) — cite as tools da lista acima com 1 linha de quando usar
  * ## Memoria persistente — oriente o que ESPECIFICAMENTE esse agente deve lembrar (preferencias de formato relacionadas ao papel, decisoes arquiteturais, nomes recorrentes)
  * ## Limites (nao faz) — cite explicitamente o que NAO faz e encaminhamento pra quem fazer
- Seja ESPECIFICO do dominio do papel. Nao gere conteudo generico.

Retorne APENAS o JSON. Nenhum texto fora, sem fences.
"""


def validate_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise ValueError(f"nome invalido '{name}': use lowercase a-z0-9- (ate 31 chars)")


def validate_mounts(write_access: list[str], read_access: list[str]) -> None:
    for m in write_access or []:
        if m not in VALID_MOUNTS:
            raise ValueError(f"write_access invalido: {m!r}. Use {VALID_MOUNTS}")
    for m in read_access or []:
        if m not in VALID_MOUNTS:
            raise ValueError(f"read_access invalido: {m!r}. Use {VALID_MOUNTS}")
    overlap = set(write_access or []) & set(read_access or [])
    if overlap:
        raise ValueError(f"mesmo mount em write e read: {overlap}")


def build_data_block(data: dict) -> str:
    """Formata os dados do wizard como bloco legivel pro prompt."""
    def _fmt_list(v):
        return ", ".join(v) if v else "(nenhum)"

    lines = [
        f"name (slug): {data.get('name')}",
        f"display_name: {data.get('display_name')}",
        f"description (uma linha): {data.get('description')}",
        "",
        "responsabilidades (o que faz):",
        data.get("responsibilities", "").strip() or "(nao especificado)",
        "",
        "o que NAO faz:",
        data.get("non_responsibilities", "").strip() or "(nao especificado)",
        "",
        f"estilo / tom: {data.get('style', '').strip() or '(nao especificado)'}",
        "",
        f"participa de workflow multi-agente (usa complete_phase): {data.get('workflow_participant')}",
        f"precisa escrever em: {_fmt_list(data.get('write_access') or [])}",
        f"precisa ler: {_fmt_list(data.get('read_access') or [])}",
        f"precisa rodar comandos (Bash): {data.get('needs_bash')}",
        f"precisa buscar na web: {data.get('needs_web')}",
    ]
    return "\n".join(lines)


async def generate_draft(data: dict) -> dict:
    """Chama claude -p via docker exec em agent-debug. Retorna dict {yaml_entry, claude_md}."""
    name = data.get("name", "")
    validate_name(name)
    validate_mounts(data.get("write_access") or [], data.get("read_access") or [])

    prompt = GENERATION_PROMPT.format(data_block=build_data_block(data))

    client = docker.from_env()
    try:
        container = client.containers.get(EXEC_CONTAINER)
    except docker.errors.NotFound:
        raise RuntimeError(
            f"container {EXEC_CONTAINER} nao encontrado — precisa de um container "
            "de agente rodando pra gerar o draft (claude CLI + creds). "
            "Suba a stack ou ajuste HIRE_AGENT_CONTAINER no env."
        )

    # exec_run com stdin=False; claude -p recebe prompt via argv
    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    hire_model = os.environ.get("HIRE_MODEL", "")
    if hire_model:
        cmd += ["--model", hire_model]
    log.info("hire.generating", container=EXEC_CONTAINER, cmd_len=len(prompt))
    import asyncio
    def _run():
        # user="node" + environment com HOME correto pro claude CLI achar credentials
        return container.exec_run(
            cmd,
            stdout=True,
            stderr=True,
            demux=True,
            user="node",
            environment={"HOME": "/home/node"},
        )
    rc_and_out = await asyncio.to_thread(_run)
    rc = rc_and_out.exit_code
    stdout, stderr = rc_and_out.output if rc_and_out.output else (b"", b"")
    stdout_s = (stdout or b"").decode("utf-8", errors="replace")
    stderr_s = (stderr or b"").decode("utf-8", errors="replace")
    if rc != 0:
        log.error("hire.exec_failed", rc=rc, stderr=stderr_s[:500])
        raise RuntimeError(f"claude falhou (rc={rc}): {stderr_s[:500]}")

    # claude --output-format json retorna envelope; extrai o `result` que eh texto do modelo
    try:
        envelope = json.loads(stdout_s)
        result_text = envelope.get("result", "").strip()
    except Exception:
        result_text = stdout_s.strip()

    # Remove fences markdown se vieram, e depois parse JSON interno
    if result_text.startswith("```"):
        result_text = re.sub(r"^```(?:json)?\s*", "", result_text)
        result_text = re.sub(r"\s*```$", "", result_text)

    try:
        parsed = json.loads(result_text)
    except Exception:
        log.error("hire.parse_failed", preview=result_text[:400])
        raise RuntimeError(
            "Claude retornou formato invalido — esperava JSON. "
            "Preview: " + result_text[:300]
        )

    entry = parsed.get("entry")
    claude_md = parsed.get("claude_md", "").strip()
    if not isinstance(entry, dict) or not claude_md:
        raise RuntimeError("Resposta sem entry (objeto) ou claude_md")

    # Forca name canonica (o que o usuario digitou, nao o que Claude inventou)
    entry["name"] = name
    # Se Claude esqueceu streams, default pra [name]
    if not entry.get("streams"):
        entry["streams"] = [name]

    # Serializa entry pra YAML valido. safe_dump gera `agents:\n- name:` (indent 0).
    # Nos queremos apenas o item, indentado 2 espacos (pra ficar abaixo de
    # `agents:` ja existente no arquivo).
    yaml_text = yaml.safe_dump({"agents": [entry]}, sort_keys=False, allow_unicode=True, default_flow_style=False)
    body_lines = yaml_text.splitlines()[1:]  # remove "agents:"
    # Re-indenta cada linha em +2 espacos
    yaml_entry = "\n".join(("  " + l) if l else "" for l in body_lines).rstrip()

    log.info("hire.draft_ok", name=name, yaml_len=len(yaml_entry), md_len=len(claude_md))
    return {
        "yaml_entry": yaml_entry,
        "claude_md": claude_md,
        "name": name,
        "warning": None,
    }


async def apply_hire(payload: dict) -> dict:
    """Grava CLAUDE.md + anexa entry em agents.yaml + roda reconcile."""
    name = payload.get("name", "").strip()
    # NAO usar .strip() no yaml_entry — remove os 2 espacos iniciais que
    # sao parte do indent de item de lista. So rstrip.
    yaml_entry = (payload.get("yaml_entry") or "").rstrip()
    claude_md = (payload.get("claude_md") or "").strip()
    validate_name(name)
    if not yaml_entry or not claude_md:
        raise ValueError("yaml_entry e claude_md obrigatorios")

    # Ja existe no agents.yaml?
    if AGENTS_YAML.exists():
        existing = AGENTS_YAML.read_text(encoding="utf-8")
        if re.search(rf"^\s*-\s*name:\s*{re.escape(name)}\s*$", existing, re.MULTILINE):
            raise ValueError(f"agente '{name}' ja existe em agents.yaml")

    # Normaliza yaml_entry: parse + re-serialize com indent correto.
    # O yaml_entry chega como item de lista, indentado 2 espacos (pra ser
    # anexado sob `agents:`). Pra parsear como root YAML, desindenta primeiro.
    try:
        lines = yaml_entry.splitlines()
        nonempty = [l for l in lines if l.strip()]
        common_indent = min((len(l) - len(l.lstrip(" "))) for l in nonempty) if nonempty else 0
        dedented = "\n".join(l[common_indent:] if len(l) >= common_indent else l for l in lines)
        parsed = yaml.safe_load(dedented)
        if isinstance(parsed, list) and parsed:
            entry_dict = parsed[0]
        elif isinstance(parsed, dict):
            entry_dict = parsed
        else:
            raise ValueError("yaml_entry nao eh uma lista nem dict apos parse")
    except Exception as e:
        raise ValueError(f"yaml_entry nao eh YAML valido: {e}")

    # Re-serializa com indent controlado
    yaml_text = yaml.safe_dump({"agents": [entry_dict]}, sort_keys=False, allow_unicode=True, default_flow_style=False)
    body_lines = yaml_text.splitlines()[1:]
    yaml_entry_clean = "\n".join(("  " + l) if l else "" for l in body_lines).rstrip()

    # Cria dir + CLAUDE.md
    agent_dir = AGENTS_DIR / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "knowledge").mkdir(exist_ok=True)
    (agent_dir / "pending_questions").mkdir(exist_ok=True)
    (agent_dir / "CLAUDE.md").write_text(claude_md, encoding="utf-8")

    # Anexa yaml_entry normalizado no agents.yaml
    with AGENTS_YAML.open("a", encoding="utf-8") as f:
        f.write("\n" + yaml_entry_clean + "\n")

    log.info("hire.files_written", name=name)

    # Dispara reconcile via container efemero (mesma abordagem do scripts/reconcile.sh)
    client = docker.from_env()
    try:
        output = client.containers.run(
            image="python:3.11-slim",
            command=["sh", "-c", "pip install -q pyyaml requests >/dev/null 2>&1 && python framework/scripts/reconcile.py --no-up"],
            volumes={
                PROJECT_ROOT_HOST: {"bind": "/work", "mode": "rw"},
            },
            working_dir="/work",
            environment={
                "PROJECT_ROOT": "/work",
                **_dot_env(),
            },
            network_mode="host",
            remove=True,
            stdout=True,
            stderr=True,
        )
        reconcile_log = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
    except Exception as e:
        log.exception("hire.reconcile_failed", name=name)
        raise RuntimeError(f"CLAUDE.md + yaml criados, mas reconcile falhou: {e}")

    # Depois do reconcile, sobe o container novo
    try:
        # docker compose up -d precisa do cli (nao temos). Usa docker SDK:
        # acha services do override e starts. Mais simples: spawn python:3.11
        # com docker-cli? Overkill. Deixa o usuario rodar `make up` ou
        # `docker compose up -d agent-<name>` depois.
        pass
    except Exception:
        pass

    log.info("hire.apply_ok", name=name)
    return {
        "ok": True,
        "name": name,
        "reconcile_log": reconcile_log[-2000:] if reconcile_log else "",
        "note": f"Agente registrado. Para subir o container: docker compose up -d agent-{name}",
    }


def _dot_env() -> dict:
    """Le .env do workspace pra injetar no container de reconcile."""
    env: dict[str, str] = {}
    p = Path("/workspace/.env")
    if not p.exists():
        return env
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env
