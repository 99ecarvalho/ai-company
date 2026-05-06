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
sugere `COMPOSE_PROJECT_NAME` e `WEB_PORT` distintos. Postgres, volumes
e rede ficam namespaced automaticamente via Docker; cada pasta tem seu
próprio `instance/`. `~/.claude/` é compartilhado por máquina (plano
MAX é 1 por host). Paths customizáveis (`AGENTS_DIR`, `COMPANY_DIR`,
`REPOS_DIR`, etc.) permitem mover qualquer parte do estado pra fora
do framework — ver tabela abaixo.

---

## Configuração (`.env`)

`make install` gera o `.env` interativamente; pra inspecionar/editar
manualmente, ver [framework/examples/.env.example](framework/examples/.env.example).
Tudo que tem default funciona out-of-the-box; só os campos sem default
precisam ser preenchidos pra usar o feature correspondente.

### Postgres / storage

| Variável | Default | Descrição |
|---|---|---|
| `POSTGRES_DB` | `agent_framework` | Nome do database. |
| `POSTGRES_USER` | `agent_framework` | Usuário do Postgres. |
| `POSTGRES_PASSWORD` | _(gerado pelo bootstrap)_ | Senha. |

### PWA / auth

| Variável | Default | Descrição |
|---|---|---|
| `WEB_PORT` | `9090` | Porta exposta no host. Único por instância no mesmo host. |
| `ADMIN_EMAIL` | `admin@example.com` | E-mail do humano principal (login). |
| `ADMIN_PASSWORD` | _(vazio)_ | Plaintext; web bcrypta no startup. Setar pra forçar login. |
| `WEB_AUTH_DEV_BYPASS` | `1` | DEV: admin implícito sem cookie/token. **Setar vazio em prod.** |
| `WEB_COOKIE_SECURE` | _(vazio)_ | `1` quando servir com HTTPS (cookies marcados Secure). |
| `WEB_DEFAULT_STREAM` | _(vazio)_ | Stream pré-selecionada no CapturePanel. Vazio = usuário escolhe. |

### Service tokens (broker interno)

| Variável | Default | Descrição |
|---|---|---|
| `ORCHESTRATOR_TOKEN` | _(gerado pelo bootstrap)_ | Auth do reactor no broker. |
| `SCHEDULER_TOKEN` | _(gerado pelo bootstrap)_ | Auth do scheduler no broker. |
| `AGENT_<NAME>_TOKEN` | _(gerado pelo reconcile)_ | Token de cada agente, automático. Não editar à mão. |

### Web push (VAPID)

Gere com `python3 framework/scripts/generate-vapid.py`.

| Variável | Default | Descrição |
|---|---|---|
| `VAPID_PUBLIC_KEY` | _(vazio)_ | Public key VAPID. Sem isso, push notifications não funcionam. |
| `VAPID_PRIVATE_KEY` | _(vazio)_ | Private key VAPID. |

### Transcriber (faster-whisper)

| Variável | Default | Descrição |
|---|---|---|
| `WHISPER_MODEL` | `large-v3` | Tamanho do modelo. Sem GPU, installer ajusta pra `base`. |
| `WHISPER_DEVICE` | `cuda` | `cuda` ou `cpu`. |
| `WHISPER_COMPUTE_TYPE` | `float16` | `float16` (GPU) ou `int8` (CPU). |
| `WHISPER_LANGUAGE` | _(vazio = autodetect)_ | Force idioma (`pt`, `en`, etc.). |

### Deploy / multi-instância

| Variável | Default | Descrição |
|---|---|---|
| `TZ` | `America/Sao_Paulo` | Timezone aplicado a todos os containers. |
| `COMPOSE_PROJECT_NAME` | `company-agents` | Namespace de containers/volumes/redes. **Único por instância no mesmo host.** |
| `COMPOSE_PROFILES` | _(vazio)_ | Setar `tunnel` pra subir cloudflared automático em todo `compose up`. |
| `DOCKER_CONFIG` | _(vazio = `~/.docker`)_ | Aponte pra `./docker/` se quiser credenciais isoladas (BYOI com registry privado). |

### Paths customizáveis (host)

Defaults mantêm tudo em `./instance/`. Aponte pra fora pra mover estado pra disco/NAS separado, versionar config em repo próprio, etc.

| Variável | Default | Descrição |
|---|---|---|
| `AGENTS_DIR` | `./instance/agents` | Config dos agentes (`agents.yaml`, `<nome>/CLAUDE.md`, `schedule.yaml`). |
| `COMPANY_DIR` | `./instance/company` | Contexto da empresa (`CONTEXT.md`, `workflow.md`, `tasks/`, `backlog/`). |
| `REPOS_DIR` | `./instance/repos` | Repos de código consumidos pelos agentes. **Recomendado fora do framework.** |
| `SESSIONS_DIR` | `./instance/sessions` | Cwd runtime por topic/task de cada agente (`<agent>/<topic-slug>/`). |
| `BACKUPS_DIR` | `./instance/backups` | Output de `framework/scripts/backup.sh`. |
| `HOOKS_DIR` | `./instance/hooks` | Scripts de hooks do Claude Code referenciados em `hooks_defaults`. |
| `INSTANCE_WEB_DIR` | `./instance/web` | Override de assets da PWA (custom icons em `icons/`). |
| `WORKTREES_DIR` | `/workspace/worktrees` | Onde a tool `create_worktree` materializa worktrees (path **dentro** do container). Mantido fora de `REPOS_DIR` pra não poluir `git status`. |

### Capability `mysql-producao` (opcional)

Preencher só se algum agente declarar `capabilities: [mysql-producao]`.

| Variável | Default | Descrição |
|---|---|---|
| `DB_PROD_HOST` | _(vazio)_ | Host do MySQL. Use `host.docker.internal` se atrás de túnel SSH local. |
| `DB_PROD_PORT` | `3306` | Porta. |
| `DB_PROD_USER` | _(vazio)_ | User read-only. |
| `DB_PROD_PASS` | _(vazio)_ | Senha. |
| `DB_PROD_NAME` | _(vazio)_ | Database. |

### Capability `sentry` (opcional)

Preencher só se algum agente declarar `capabilities: [sentry]`.

| Variável | Default | Descrição |
|---|---|---|
| `SENTRY_AUTH_TOKEN` | _(vazio)_ | Auth token (mesmo nome usado por sentry-cli/SDKs). |
| `SENTRY_HOST` | _(vazio = sentry.io)_ | Hostname-only se self-hosted. |

### Git push / MR-PR

Injetadas em todos os agentes pelo reconcile; agentes consultivos simplesmente não invocam `git push`. Imagem base traz `glab` e `gh` pré-instalados — framework é agnóstico.

| Variável | Default | Descrição |
|---|---|---|
| `GITLAB_TOKEN` | _(vazio)_ | PAT com scope `api` + `write_repository`. |
| `GITLAB_HOST` | _(vazio = gitlab.com)_ | Self-hosted: `gitlab.empresa.com`. |
| `GH_TOKEN` | _(vazio)_ | PAT GitHub com scope `repo`. |
| `GIT_AUTHOR_NAME` | `agent-framework` | Identidade nos commits criados pelos agentes. |
| `GIT_AUTHOR_EMAIL` | `agents@local` | Idem. |

### PWA manifest

Lido em runtime; mudar requer `docker compose restart web` (sem rebuild).

| Variável | Default | Descrição |
|---|---|---|
| `PWA_NAME` | `Agents` | Nome longo na home screen. |
| `PWA_SHORT_NAME` | `Agents` | Nome curto (ícone). |
| `PWA_DESCRIPTION` | `Multi-agent orchestration` | Description. |
| `PWA_THEME_COLOR` | `#0b1220` | Cor da status bar mobile. |
| `PWA_BACKGROUND_COLOR` | `#0b1220` | Cor do splash screen. |
| `PWA_ID` | `/agent-framework` | ID único pro browser distinguir instâncias. |
| `PWA_LANG` | `en` | `pt-BR`, `en`, etc. |
| `PWA_START_URL` | `/` | Rota inicial ao abrir o PWA instalado. |
| `PWA_ORIENTATION` | `any` | `any`/`portrait`/`landscape`/`portrait-primary`/etc. Só efeito em standalone. |

### Cloudflare tunnel (opcional)

Expõe o web container num hostname público via Cloudflare edge — HTTPS automático, sem DNS/port-forward, funciona atrás de NAT.

| Variável | Default | Descrição |
|---|---|---|
| `CF_TUNNEL_TOKEN` | _(vazio)_ | Connector token do tunnel criado no dashboard Cloudflare. Sem isso, serviço não sobe. |

### Outros

| Variável | Default | Descrição |
|---|---|---|
| `TERMINAL_NOTIFY_STREAM` | _(vazio)_ | Stream agregador de notificações terminais (`done`/`halt`/`human_review`). Vazio = só posta na conversa de origem. |
| `CLAUDE_MOCK` | _(vazio)_ | `1` faz `claude_runner` devolver reply scriptado sem invocar a CLI. Usado pela suite Playwright. |
| `CLAUDE_MOCK_REPLY` | _(vazio)_ | Customiza o texto do reply mockado. |

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

1. **Isolar tasks paralelas em git worktrees.** Agente chama a tool MCP
   `create_worktree(task_slug, repo)`. O framework cria a worktree em
   `${WORKTREES_DIR:-/workspace/worktrees}/<repo>/<task_slug>/` (**fora**
   da árvore do repo canônico em `repos/<repo>/` — worktrees nunca poluem
   `git status` do repo principal), com branch `task/<task_slug>` e
   baseline em `origin/<default-branch>` HEAD (override via args). Estado
   persistido em `tasks.worktrees`; chamada é idempotente (mesma task +
   repo + branch retorna a worktree existente). Cleanup no fim da task
   via `cleanup_worktrees(task_slug)` — remove via `git worktree remove`
   + `prune` + `DELETE FROM tasks.worktrees`.
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

## Tools MCP do framework

Todo agente roda um servidor MCP **in-process** que expõe as tools abaixo
sob o prefixo `mcp__agent_framework__*`. Cada tool aparece pro agente como
uma function call do Claude — habilitar = listar em `allowed_tools` no
`agents.yaml` (ou herdar do default da imagem). Tools de **capability**
(playwright/sentry/mysql) são separadas e descritas em [Estendendo agentes](#estendendo-agentes-além-do-básico).

### Comunicação humano ↔ agente

| Tool | O que faz |
|---|---|
| `ask_human` | Pergunta ao humano e **bloqueia** indefinidamente até resposta. Use pra decisões que mudam escopo/arquitetura ou ambiguidades sem resposta objetiva. PWA destaca a conversa com badge "needs you". |
| `notify_human` | Posta mensagem na conv atual (fire-and-forget). Não bloqueia, não força atenção — vira bolinha discreta na sidebar. |
| `archive_conversation` | Arquiva a conv atual (some da inbox; histórico preservado, reabrível). |

### Coordenação entre agentes

| Tool | O que faz |
|---|---|
| `ask_agent` | Pausa e pergunta a OUTRO agente. Cria conv-filha; o agente alvo recebe a pergunta como prompt e o asker espera a resposta síncrona. Hierarquia raiz→filha enforced no broker. Habilitar requer `can_ask: [<target>]` em `agent_policies`. |
| `ask_agents_many` | Múltiplos `ask_agent`/dispatches em **paralelo** numa única chamada. Útil pra fan-out (consultar 3 especialistas simultaneamente). |

### Lifecycle de task

| Tool | O que faz |
|---|---|
| `complete_phase` | Marca o step atual como concluído e faz handoff. `next=<próximo-step>` continua a task; `next=done`/`halt`/`human_review` é terminal (orchestrator muda status). |
| `get_task_state` | Lê estado estruturado da task atual: slug, title, workflow, status, phases concluídas, worktrees ativas, baselines. Substitui o ritual de inspecionar metadata.yaml. |
| `task_list` | Lista tasks ativas (default) ou inclui arquivadas. Filtros por status/agente. |
| `reopen_task` | Reabre task em estado terminal (done/blocked/human_review). Restrição: só quando o humano pedir explicitamente. |

### Worktrees

| Tool | O que faz |
|---|---|
| `create_worktree` | Cria worktree isolada em `${WORKTREES_DIR}/<repo>/<task_slug>/` + registra em `tasks.worktrees`. Idempotente (mesmo task+repo+branch reaproveita). Branch default: `task/<slug>`. Baseline default: `origin/<default-branch>` HEAD. |
| `cleanup_worktrees` | Remove todas worktrees registradas pra task: `git worktree remove --force` + `prune` + delete da row. Falhas não são mascaradas. |

### Memória persistente (por agente)

| Tool | O que faz |
|---|---|
| `memory_save` | Salva fato de longo prazo na memória **deste** agente (não compartilhado entre agentes). Tag obrigatória, key opcional. |
| `memory_recall` | Busca semântica + textual na memória do agente. Ordenada por relevância, com `limit`. |
| `memory_list` | Lista facts recentes sem query — útil pra auditar o que foi acumulado. |
| `memory_edit` | Atualiza fact existente (falha se key não existe). |
| `memory_delete` | Remove fact (hard delete; sem undo). |

### Backlog (fila compartilhada de ideias/pendências)

| Tool | O que faz |
|---|---|
| `backlog_add` | Registra ideia/item pendente. Compartilhado entre agentes (não é por agente). |
| `backlog_list` | Lista ordenado por prioridade desc, depois created_at. Filtros por status/owner. |
| `backlog_update` | Atualiza campos de item existente; campos omitidos preservam valor. |
| `backlog_promote` | Promove item → task ativa. Cria task com workflow declarado e dispara fase inicial. |

### Scheduler (cron jobs DB-backed)

| Tool | O que faz |
|---|---|
| `schedule_add` | Cria job recorrente (cron expression). Cria conv própria a cada disparo. |
| `schedule_list` | Lista jobs custom criados via MCP/PWA (não inclui overrides nativos definidos no YAML). |
| `schedule_update` | Edita job existente; campos omitidos preservados. |
| `schedule_remove` | Deleta job permanentemente. |

### Skills (Claude Code skills por agente)

| Tool | O que faz |
|---|---|
| `save_skill` | Persiste skill na biblioteca **deste** agente (`/app/agents/<name>/skills/`). Cada agente tem sua própria. |
| `list_skills` | Lista skills do agente (name + description). |
| `delete_skill` | Remove skill (hard delete). |

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
