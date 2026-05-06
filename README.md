# agent-framework — Empresa virtual com agentes Claude Code

Framework self-hosted onde você define "funcionários" (agentes Claude Code)
em um único YAML, e o sistema cuida do resto: messaging interno (Postgres +
broker HTTP), transcrição de áudio (faster-whisper), push VAPID, e uma PWA
pra conversar com todos eles de um lugar só.

Você não edita Docker YAML — edita [instance/agents/agents.yaml](instance/agents/agents.yaml)
e roda `make reconcile`.

> Para Claude Code: [CLAUDE.md](CLAUDE.md) é auto-loaded.

---

## Pré-requisitos

- **Docker Engine 25+** com `docker compose` v2
- **Claude Code CLI logado** no host (`claude` uma vez → cria `~/.claude/`)
- **Linux ou WSL2** (macOS deve funcionar)
- **Opcional — GPU NVIDIA** pra transcrição rápida. Sem GPU, o installer
  escolhe modelo menor automaticamente.

---

## Instalar

```bash
git clone <repo-url> agent-framework
cd agent-framework
make install
```

O wizard interativo pergunta nome da empresa, porta, credenciais admin,
device do Whisper, e (se aplicável) registry privado. Gera `.env`,
builda imagens, aplica migrations, sobe a stack, espera `/health`. Abra
`http://localhost:<porta>` e siga o onboarding.

### Multi-empresa no mesmo host

Copie o repo pra outra pasta e rode `make install` lá — o installer
vai sugerir `COMPOSE_PROJECT_NAME` e `WEB_PORT` distintos. Cada pasta
fica com:

- **`instance/`** próprio (agents.yaml, memória, mensagens, tudo).
- **Postgres, volumes, rede** namespaced automaticamente via Docker.
- **Paths customizáveis via `.env`** — defaults mantêm tudo em `./instance/`,
  mas você pode apontar pra fora:
  - `REPOS_DIR` — repos de código dos agentes (ex: `../gitlab-acme`).
    Evita aninhar múltiplos gits no workspace do framework.
  - `AGENTS_DIR` — config dos agentes (`agents.yaml`, `<nome>/CLAUDE.md`,
    `schedule.yaml`). Útil pra versionar config privada em repo próprio.
  - `COMPANY_DIR` — contexto da empresa (`CONTEXT.md`, `workflow.md`,
    `tasks/`, `backlog/`). Mesma motivação.
  - `BACKUPS_DIR` — output de `framework/scripts/backup.sh`. Aponte pra
    NAS/disco externo se quiser retenção fora do framework.
- **`DOCKER_CONFIG=./docker`** (opcional) — se a empresa usa imagem
  privada de registry onde você tem outras contas logadas, o installer
  cria um `./docker/` local com credenciais isoladas (sem colidir com
  `~/.docker/config.json` global).

`~/.claude/` é compartilhado por máquina (plano MAX é 1 por host).

---

## Uso dia-a-dia

```bash
# Setup
make install              # wizard interativo (1ª instalação)
make help                 # lista todos os targets

# Stack
make up                   # sobe tudo (docker compose up -d)
make down                 # derruba (preserva volumes)
make restart              # down + up
make build                # rebuild das imagens (agent + web + transcriber + watchdog)
make healthcheck          # sanity check (containers + web + db + transcriber)
make test                 # pytest do framework (dentro do container web)

# Agentes / config
make reconcile            # aplica instance/agents/agents.yaml (bots + streams + .env + override + up)
make reconcile-dry        # mostra o que o reconcile faria, sem aplicar
make new-agent NAME=x DISPLAY="X"   # scaffold de agente novo no agents.yaml
make reset-agent-<nome>   # limpa sessions de um agente (ex: make reset-agent-inbox)

# Logs
make logs                 # tail de toda a stack
make logs-<servico>       # logs de um serviço específico (ex: make logs-inbox)
make logs-all             # agregado com pretty-print JSON + cores por service (LEVEL=error filtra)
make logs-errors          # só warnings+errors
make shell-<servico>      # bash dentro de um serviço (ex: make shell-inbox)

# Banco / migrations
make migrate              # aplica migrations pendentes (normalmente roda sozinho no boot do web)
make migrate-status       # lista aplicadas/pendentes
make migrate-baseline V=N # marca como aplicada sem executar
make tasks-migrate        # backfill idempotente company/tasks/ → Postgres

# Reset
make reset-instance       # zera DB + sessions preservando config (pede confirmação)
make reset-instance-yes   # igual sem prompt (CI/scripts)
```

Pelo PWA: **👔** no header da sidebar abre o hire wizard (form + preview
do YAML/CLAUDE.md gerados pelo Claude). **🔔** ativa push.

Push chega **só** quando um agente faz `ask_human` (está bloqueado
aguardando você). Replies normais não geram push — aparecem como
**bolinha discreta** na sidebar até você abrir. Conversas com
`ask_human` pendente ganham **destaque forte** (badge amber "needs
you" + borda lateral).

---

## Estendendo agentes além do básico

Dois caminhos quando um agente precisa de algo que não está na imagem
padrão:

**Tools complexas via MCP lateral** (`capabilities:` no agents.yaml).
Um container separado roda o MCP server; os agentes que declaram a
capability ganham as tools automaticamente. Implementadas hoje:

- `playwright` — navegador automatizado (Chromium headless via
  [@playwright/mcp](https://github.com/microsoft/playwright-mcp)). Uma
  instância serve N agentes. Útil pra testes visuais, tutoriais,
  scraping. Habilite com `capabilities: [playwright]` e adicione
  `mcp__playwright__*` em `allowed_tools`.
- `sentry` — issues, releases e events do Sentry via
  [@sentry/mcp-server](https://www.npmjs.com/package/@sentry/mcp-server).
  Roda stdio dentro do próprio container do agente (sem sidecar).
  Preencha `SENTRY_AUTH_TOKEN` no `.env` (e `SENTRY_HOST` se for
  self-hosted). Habilite com `capabilities: [sentry]` e adicione
  `mcp__sentry__*` em `allowed_tools`.
- `mysql-producao` — acesso **read-only** a MySQL via
  [`@benborla29/mcp-server-mysql`](https://github.com/benborla/mcp-server-mysql)
  bridgeado stdio→HTTP por `supergateway`. Única tool exposta é
  `mysql_query` com `readOnlyHint`. Preencha `DB_PROD_*` no `.env` (a
  capability aceita conexão via `host.docker.internal` se o banco está
  atrás de túnel SSH no host). Habilite com `capabilities: [mysql-producao]`
  e adicione `mcp__mysql-producao__*` em `allowed_tools`.

Adicionar nova capability = 1 entry em `MCP_CAPABILITIES` (em
[reconcile.py](framework/scripts/reconcile.py)) + 1 Dockerfile em
[framework/docker/](framework/docker/).

**CLI nativa via imagem própria** (`image:` no agents.yaml, aka BYOI).
Quando a tool é executada via `Bash()` pelo Claude (`phpstan`,
`terraform`, `kubectl`), precisa estar no PATH do container. Monte sua
imagem estendendo [framework/docker/agent.Dockerfile](framework/docker/agent.Dockerfile)
(precisa herdar o runner Python + entrypoint), publique num registry,
e aponte:

```yaml
- name: php-reviewer
  image: registry.gitlab.com/acme/my-php-agent:v1
  # ... resto igual
```

Se o registry é privado e você tem várias contas, rode
`DOCKER_CONFIG=./docker docker login <registry>` (o installer faz
isso se você responder "sim" pro prompt de imagem privada).

**O que já vem na imagem base** (pra você não re-instalar): `git`,
`openssh-client`, `glab` (GitLab CLI), `gh` (GitHub CLI), `jq`,
`curl`, `postgresql-client-16`, `python3`+`venv`, Node 20, Claude Code
CLI. Ver [framework/docker/agent.Dockerfile](framework/docker/agent.Dockerfile).

### Fluxo recomendado de código: worktree + MR/PR + anti-push-main

O framework é **agnóstico** (GitLab ou GitHub) e oferece as peças;
a instância decide se adota o padrão todo ou parte. Recomendado:

1. **Isolar tasks paralelas em git worktrees.** Executor roda
   `git worktree add -b task/<slug> .worktrees/<slug> origin/main` no
   repo alvo. Duas tasks no mesmo repo não colidem.
2. **Nunca push direto em `main`.** Um PreToolUse hook do Claude Code
   bloqueia `git push origin main|master` quando chamado pelos agentes.
   Script genérico em `framework/examples/hooks/block-push-main.sh`
   — copie pra `${HOOKS_DIR:-instance/hooks}/` e referencie em
   `hooks_defaults` de `agents.yaml`. O bloqueio **não** afeta push do
   host (humano faz livre); só intercepta Bash vindo dos agentes.
3. **Abrir MR/PR no fim.** Agente detecta host via `git remote -v`:
   GitLab → `glab mr create --target-branch main ...`, GitHub →
   `gh pr create --base main ...`. Merge é decisão humana.

Tokens necessários no `.env`: `GITLAB_TOKEN` (scope `api` +
`write_repository`) ou `GH_TOKEN` (scope `repo`), mais
`GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL`. Injetados em todos os agentes
pelo reconcile — só quem realmente usa (executores, revisor) invoca.

---

## Arquitetura (resumo)

- **Agentes** = containers Python com subprocess `claude -p` + MCP server
  in-process (`ask_human`, `ask_agent`, `complete_phase`, `memory_*`).
- **web** = FastAPI + PWA SvelteKit (bundle ~300KB, zero Node em runtime).
  Broker HTTP sobre Postgres.
- **Postgres** = storage único. `pg_notify` dispara LISTEN nos agentes.
  Schema versionado em [framework/db/migrations/](framework/db/migrations/);
  runner aplica pendentes no entrypoint do `web` (zero passo manual).
- **orchestrator-reactor + scheduler** = handoffs e cron jobs.
- **transcriber** = faster-whisper (GPU ou CPU).
- **watchdog** = monitora `instance/heartbeats/` e restarta agentes stale.
- **MCPs laterais** (opt-in via `capabilities:`) = `playwright-mcp` (navegador
  automatizado, container compartilhado), `sentry` (stdio in-process via npx),
  `mysql-producao-mcp` (query read-only em DB de produção via
  `host.docker.internal` — usa túnel SSH no host quando o banco não é exposto
  diretamente).

---

## Troubleshooting

**Claude CLI expirou.** `claude` no host → refaz auth. Agentes pegam na
próxima execução (bind mount RO de `~/.claude/`).

**Sem GPU.** O installer detecta e ajusta Whisper pra CPU (modelo `base`,
`int8`). Pra desligar transcrição de vez, remova o service `transcriber`
de [docker-compose.yml](docker-compose.yml).

**Push blocked.** Browser recusou permissão. Settings → site → permita
notifications → clique 🔔 de novo.

**Reset nuclear.** `docker compose down -v` apaga Postgres inteiro
(mensagens, memória, telemetria, push subs). No próximo `up`, o
entrypoint do `web` roda migrations do zero.

**Migration schema nova.** Crie `framework/db/migrations/NNN_desc.sql`,
rebuild do `web` (`docker compose build web`), `docker compose up -d web`.
O entrypoint aplica. `make migrate-status` lista aplicadas/pendentes.

---

## Estrutura do projeto

```
agent-framework/
├── framework/          # CÓDIGO tracked
│   ├── bots/           # runner dos agentes (Python asyncio + MCP)
│   ├── web/            # FastAPI + SvelteKit
│   ├── orchestrator/   # reactor + scheduler
│   ├── db/migrations/  # schema versionado (NNN_nome.sql)
│   ├── docker/         # Dockerfiles (agent, playwright-mcp, mysql-mcp)
│   ├── scripts/        # install, reconcile, bootstrap-env, migrate
│   └── examples/       # defaults genéricos copiados no bootstrap
├── instance/           # ESTADO gitignored (agents.yaml, company/, ...)
├── CLAUDE.md           # auto-loaded pelo Claude Code
├── Makefile
└── docker-compose.yml
```

Regra: nada em `instance/` vai pro repo. `make install` popula
`instance/` a partir de `framework/examples/`. Repos consumidos pelos
agentes podem ficar em `instance/repos/` (default) ou fora (via
`REPOS_DIR` no `.env`) — **recomendado fora** pra não aninhar N gits
no workspace do editor.

---

## Docs

- [CLAUDE.md](CLAUDE.md) — contexto auto-carregado pelo Claude Code (estrutura, regras, system prompt dos agentes, acesso rápido).
- [framework/web/frontend/MOBILE.md](framework/web/frontend/MOBILE.md) — armadilhas de layout/UI mobile e cache da PWA.
