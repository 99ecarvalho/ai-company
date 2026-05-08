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

## O que vem no fresh install

`make install` (= `bootstrap-env.sh` + reconcile + build + up) deixa
sua instância com o seguinte estado **runnable end-to-end**:

### Em `instance/agents/`

- **`agents.yaml`** ← `framework/examples/agents.yaml.example`. Define **5 agentes** que cobrem os workflows shipados:
  - `triager` — primeiro contato + wrap-up.
  - `planner` — desenha planos técnicos (Opus, effort high).
  - `executor` — implementa código (Bash + worktrees, Opus, effort high).
  - `reviewer` — revisa código + roda testes.
  - `researcher` — investigação sem código (WebFetch/WebSearch).
  - **Hook `block-push-main` ativo por default** — bloqueia agentes de pushar em `main`/`master`.
- **`<nome>/CLAUDE.md`** ← gerado por agente pelo reconcile a partir do template (identidade + descrição), pronto pra editar.

### Em `instance/company/`

- **`CONTEXT.md`** ← `framework/templates/CONTEXT.md.example`. Skeleton com placeholders pra preencher (nome, setor, missão, convenções de data/moeda/idioma). **Importante**: este arquivo é injetado no system prompt de TODO agente em TODA invocação — fonte de verdade compartilhada.
- **`workflows.yaml`** ← `framework/examples/workflows.yaml.example`. **2 workflows** prontos:
  - `default` (5 steps): `intake` → `plan` → `build` → `review` → `wrap`.
  - `research` (3 steps): `intake` → `investigate` → `wrap`.
- **`philosophy.md`** ← stub vazio com hint pra ver os templates abaixo.

### Em `framework/templates/philosophies/` (não copiados — você escolhe)

8 templates de filosofia operacional disponíveis pra `cp` em `instance/company/philosophy.md`. Cada um segue o mesmo skeleton (princípios-chave, estrutura de tasks, vocabulário, agentes arquetípicos, cadência) pra que o claude_runner injete consistentemente:

- `tdd.md` — Test-Driven Development
- `bdd.md` — Behavior-Driven Development
- `sdd.md` — Spec-Driven Development
- `ddd.md` — Domain-Driven Design (bounded contexts → agents)
- `tbd.md` — Trunk-Based Development
- `hexagonal.md` — Hexagonal / Clean Architecture
- `context-management.md` — disciplina de what-goes-into-prompt
- `custom.md` — esqueleto vazio pra escrever do zero

Misture várias se quiser — não são canônicas, são pontos de partida.

### Subpastas que NÃO são pré-criadas

`CONTEXT.md` referencia `company/ideas/`, `company/notes/`, `company/decisions/`, `company/tasks/`. Essas pastas são criadas **lazy** pelos agentes quando o caminho é usado pela primeira vez (write_access tem `company` como rw). Não precisa criar à mão.

### Em `instance/repos/` e `instance/worktrees/`

Vazias. Você popula `instance/repos/<nome>/` clonando os repos que seus agentes vão consumir (ou aponta `REPOS_DIR` pra fora do framework — recomendado pra não aninhar gits no workspace do editor). `worktrees/` é populado dinamicamente pela tool `create_worktree` quando agentes começam tasks.

### `.env`

Gerado pelo wizard interativo do `install.sh` com secrets random + perguntas (porta, admin password, device do Whisper, etc). Ver tabela completa em [Configuração (`.env`)](#configuração-env).

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

> **Default stream + push (VAPID)**: configurados pelo PWA em `Settings → System`. Persistem em `web.app_settings` (DB).

### Service tokens (broker interno)

| Variável | Default | Descrição |
|---|---|---|
| `ORCHESTRATOR_TOKEN` | _(gerado pelo bootstrap)_ | Auth do reactor no broker. |
| `SCHEDULER_TOKEN` | _(gerado pelo bootstrap)_ | Auth do scheduler no broker. |
| `AGENT_<NAME>_TOKEN` | _(gerado pelo reconcile)_ | Token de cada agente, automático. Não editar à mão. |

### Transcriber (faster-whisper)

GPU é **opt-in** via `docker-compose.gpu.yml` — adicione `COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml` em `.env` pra reservar NVIDIA + flipar pra cuda/large-v3/float16. Defaults rodam em CPU em qualquer host.

| Variável | Default | Descrição |
|---|---|---|
| `WHISPER_MODEL` | `small` | Tamanho do modelo. Com GPU layer: `large-v3`. |
| `WHISPER_DEVICE` | `cpu` | `cuda` requer `docker-compose.gpu.yml` ativo. |
| `WHISPER_COMPUTE_TYPE` | `int8` | `float16` quando em GPU. |
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

### Credenciais consumidas por `capability_instances`

Variáveis lidas pelas instâncias declaradas no `capability_instances` do
`agents.yaml`. As próprias instâncias mapeiam `env: { KEY: "${VAR:-}" }`,
então preencha as `VAR` aqui só se a instância correspondente existe. Ver
[Capabilities — templates vs instances](#capabilities--templates-vs-instances)
abaixo pro mecanismo geral.

**MySQL (template `mysql`)** — preencher se você declarou uma instância
mapeando `MYSQL_HOST/USER/PASS/...` pra essas vars (ex: `mysql-producao`
mapeando `${DB_PROD_*}`).

| Variável | Default | Descrição |
|---|---|---|
| `DB_PROD_HOST` | _(vazio)_ | Host do MySQL. Use `host.docker.internal` se atrás de túnel SSH local. |
| `DB_PROD_PORT` | `3306` | Porta. |
| `DB_PROD_USER` | _(vazio)_ | User read-only (recomendado). |
| `DB_PROD_PASS` | _(vazio)_ | Senha. |
| `DB_PROD_NAME` | _(vazio)_ | Database. |

**Sentry (template `sentry`)** — preencher se você declarou a instância
`sentry` (ou outra apontando pro template `sentry`).

| Variável | Default | Descrição |
|---|---|---|
| `SENTRY_AUTH_TOKEN` | _(vazio)_ | Auth token (mesmo nome usado por sentry-cli/SDKs). Vira `SENTRY_ACCESS_TOKEN` no env do agente via mapping da instância. |
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
Cada agente declara as capabilities que quer; reconcile materializa o
MCP server (sidecar Compose ou stdio in-process via npx) e injeta as
tools no claude.

### Capabilities — templates vs instances

Capabilities resolvem em duas categorias (D-119):

- **Singletons** definidos no framework ([reconcile.py](framework/scripts/reconcile.py)
  `MCP_CAPABILITIES`). Config fixa, um por instalação. Apropriado pra
  capabilities sem creds que variam (ex: pool de browser). Hoje:
  - `playwright` — Chromium headless via [@playwright/mcp](https://github.com/microsoft/playwright-mcp).
    HTTP sidecar (uma instância serve N agentes). Habilite com
    `capabilities: [playwright]` + `mcp__playwright__*` em `allowed_tools`.

- **Templates** definidos no framework, **instâncias** declaradas em
  `instance/agents/agents.yaml` no bloco top-level `capability_instances`.
  Apropriado pra qualquer capability com creds que variam por deploy
  (token, conn URL). Cada instância tem nome único, vira o namespace MCP
  (`mcp__<name>__*`) e o nome do server. Permite múltiplas instâncias do
  mesmo template (ex: `mysql-producao` + `mysql-staging`) com creds
  distintas, sem leak de vocabulário da instância pro framework.

  Templates hoje em [reconcile.py](framework/scripts/reconcile.py)
  (`CAPABILITY_TEMPLATES`):
  - `mysql` — read-only via [@benborla29/mcp-server-mysql](https://github.com/benborla/mcp-server-mysql)
    (stdio in-process via npx). Env keys: `MYSQL_HOST`, `MYSQL_PORT`,
    `MYSQL_USER`, `MYSQL_PASS`, `MYSQL_DB`. Aceita conexão via
    `host.docker.internal` se o DB está atrás de túnel SSH no host.
  - `sentry` — issues, releases e events via [@sentry/mcp-server](https://www.npmjs.com/package/@sentry/mcp-server)
    (stdio in-process via npx). Env keys: `SENTRY_ACCESS_TOKEN`,
    `SENTRY_HOST` (vazio = SaaS sentry.io).

  Exemplo de instância (no `agents.yaml`):
  ```yaml
  capability_instances:
    mysql-producao:
      template: mysql
      env:
        MYSQL_HOST: "${DB_PROD_HOST:-}"
        MYSQL_PORT: "${DB_PROD_PORT:-3306}"
        MYSQL_USER: "${DB_PROD_USER:-}"
        MYSQL_PASS: "${DB_PROD_PASS:-}"
        MYSQL_DB:   "${DB_PROD_NAME:-}"
    sentry:
      template: sentry
      env:
        SENTRY_ACCESS_TOKEN: "${SENTRY_AUTH_TOKEN:-}"
        SENTRY_HOST:         "${SENTRY_HOST:-}"
  ```
  Agentes consomem por nome em `capabilities:` (mistura instances +
  singletons; resolve transparente):
  ```yaml
  agents:
    - name: ops
      capabilities: [mysql-producao, sentry, playwright]
      allowed_tools:
        - "mcp__mysql-producao__*"
        - mcp__sentry__list_issues  # ou "mcp__sentry__*"
        - "mcp__playwright__*"
  ```

Regra prática: **tem creds que variam → template; compute puro sem
creds → singleton**. Pra capability nova:
- Singleton: 1 entry em `MCP_CAPABILITIES` (+ Dockerfile se HTTP sidecar).
- Template: 1 entry em `CAPABILITY_TEMPLATES` + N instâncias no `agents.yaml`.

Limitação v1: **uma instância por template por agente** — env vars do
MCP server colidem se o mesmo agente tentar duas instâncias do mesmo
template. v2 futura: namespacing automático de env (`MYSQL_PRODUCAO__HOST`,
`MYSQL_STAGING__HOST`) + expansão no claude_runner.

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

## Hooks (Claude Code)

[Hooks do Claude Code](https://code.claude.com/docs/en/hooks-guide)
são shell commands que rodam em pontos da lifecycle (`PreToolUse`,
`PostToolUse`, `Stop`, `SessionStart`, `UserPromptSubmit`, etc.). O
framework expõe isso como **passthrough**: você declara hooks no
`agents.yaml`, o reconcile materializa em `.claude/settings.json` por
agente, o Claude CLI consome — framework não interpreta a semântica.

### Onde declarar

Dois níveis, concatenados por evento:

```yaml
# agents.yaml

# Aplica a TODOS os agentes (top-level)
hooks_defaults:
  PreToolUse:
    - matcher: "Bash"
      hooks:
        - type: command
          command: "/app/hooks/block-push-main.sh"

agents:
  - name: executor
    # ...
    hooks:                          # Per-agent, soma com defaults
      PreToolUse:
        - matcher: "Bash"
          hooks:
            - type: command
              command: "/app/agents/executor/hooks/block-rm-rf.sh"
      Stop:
        - hooks:
            - type: command
              command: "/app/agents/executor/hooks/notify-finish.sh"
```

### Onde colocar os scripts

Dois locais possíveis (scripts precisam estar visíveis **dentro** do container):

| Localização no host | Path no container | Quando usar |
|---|---|---|
| `${HOOKS_DIR:-./instance/hooks}/*.sh` | `/app/hooks/` (ro, todos os agentes) | Hooks de política compartilhados (bloqueios, audit log, formatters globais). |
| `${AGENTS_DIR}/<name>/hooks/*.sh` | `/app/agents/<name>/hooks/` (rw do próprio agente) | Hooks específicos de um papel/agente. |

Imagem base já vem com `jq` e `yq` pré-instalados — basta `chmod +x` no script.

### Contrato do hook

Stdin recebe um JSON do Claude Code descrevendo o evento (com `tool_name`, `tool_input`, etc.). Exit code controla:

- `exit 0` — deixa passar (tool executa normalmente).
- `exit 2` — **bloqueia** a tool. O conteúdo de stderr volta como feedback pro Claude (vira mensagem visível no contexto, não erro silencioso).

Exemplo prático shipado em [framework/examples/hooks/block-push-main.sh](framework/examples/hooks/block-push-main.sh): bloqueia `git push origin main|master` quando vem do agente; libera push pra branch feature. Cobre compostos (`cd foo && git push`), `git -C path push`, `HEAD:main`, etc. Não afeta push do humano no host.

### Aplicar mudanças em hooks

Edit em `agents.yaml` → `make reconcile` → restart do agente (`docker compose restart <name>`). **Sem rebuild de imagem** — hooks só regeneram `.claude/settings.json`.

---

## Workflows (multi-step tasks)

Workflows declaram a **lifecycle de uma task multi-agente**: quais
steps existem, qual agente executa cada um, qual artefato cada step
produz, e quais transições são válidas. Ficam em
`${COMPANY_DIR}/workflows.yaml` e são **hot-reloaded a cada `complete_phase`**
— editar não exige restart.

### Filosofia: framework agnóstico, instância declara o vocabulário

O framework conhece **apenas 3 terminais** com semântica fixa:

| Terminal | Status resultante | Quando usar |
|---|---|---|
| `done` | `done` | Sucesso. Task encerrada. |
| `halt` | `blocked` | Humano precisa destravar (ex: token expirou, decisão pendente). |
| `human_review` | `human_review` | Humano precisa decidir direção (ex: 3 caminhos válidos, sem resposta objetiva). |

**Qualquer outro nome é step livre** declarado pela sua instância. Os exemplos shipados usam `intake`/`plan`/`build`/`review`/`wrap`, mas você escolhe — pode ser `triagem`/`planejamento`/`execucao`, `discovery`/`design`/`ship`, `triage`/`develop`/`merge`, etc.

### Schema de cada step

```yaml
workflows:
  default:                  # nome do workflow — referenciado em complete_phase
    initial_step: intake    # primeiro step quando a task é criada
    steps:
      intake:
        agent: triager      # default executor (opcional — sem isso, complete_phase
                            # exige `next_agent` explícito ao apontar pra cá)
        artifact: 00-intake.md   # nome do arquivo que este step produz
                                 # (injetado no prompt do próximo agente:
                                 #  "procure por X na task")
        next: [plan, halt, human_review]  # transições válidas. complete_phase
                                          # rejeita destinos fora desta lista.
        instructions: |              # (opcional) markdown injetado no system
          ## O que fazer aqui        #  prompt do agente DURANTE este step.
          1. Ler a task...           #  Toda mecânica de fase mora aqui (gates,
          2. Classificar...          #  worktree, push, encerramento) — não nos
                                     #  CLAUDE.md dos agentes.
        overrides:                   # (opcional) sobrescreve config do agente
          model: sonnet              #  só durante este step. Útil pra triagem
          effort: low                #  em Sonnet/low e execução em Opus/high
          memory:                    #  sem ter agentes duplicados.
            auto_inject_limit: 3
      plan:
        agent: planner
        artifact: 01-plan.md
        next: [build, halt, human_review]
      # ...
```

### Como uma task é amarrada a um workflow

Na **primeira chamada** de `complete_phase` pra uma task nova, o agente passa `workflow: <name>` + `title`. O framework persiste em `tasks.tasks.workflow`; daí em diante, todo `complete_phase` consulta esse arquivo pra:

1. **Validar transição** (`next` é elegível?).
2. **Resolver agente do próximo step** (do campo `agent`, ou do `next_agent` explícito).
3. **Carregar `instructions` + `overrides`** pra montar o system prompt do próximo turno.

### Modo permissivo (sem `workflows.yaml` ou workflow não encontrado)

Framework **aceita qualquer step** mas exige `next_agent` explícito em todo `complete_phase`. Útil pra começar simples antes de declarar workflows formalmente.

### Workflows que vêm de exemplo

`framework/examples/workflows.yaml.example` é copiado pro `instance/company/workflows.yaml` no bootstrap. Define:

- **`default`** (5 steps): `intake` → `plan` → `build` → `review` → `wrap` → `done`. Fluxo linear típico, com loop opcional `review` → `build` se precisar correção.
- **`research`** (3 steps): `intake` → `investigate` → `wrap` → `done`. Pra perguntas que terminam em documento, sem código.

Os agentes referenciados (`triager`, `planner`, `executor`, `reviewer`, `researcher`) também vêm definidos no `agents.yaml.example`, então o setup é runnable end-to-end sem ajuste.

### Editor visual no PWA

- **Settings → Workflows**: edita `instructions` por step (textarea YAML), `overrides`, `next`, etc.
- **Settings → Preview**: simula a montagem do system prompt pra um step/agente específico — útil pra ver o concatenado final antes de testar com task real.

Mudanças via PWA aplicam no **próximo spawn** do agente (sem rebuild).

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
- **MCPs laterais** (opt-in via `capabilities:`) — duas categorias (D-119):
  - **Singletons** (HTTP sidecar): `playwright-mcp` (navegador automatizado,
    container compartilhado).
  - **Templates parametrizáveis** (stdio in-process via npx, instâncias
    declaradas em `capability_instances` no `agents.yaml`): `mysql`
    (read-only via `@benborla29/mcp-server-mysql` — aceita `host.docker.internal`
    se o DB está atrás de túnel SSH no host) e `sentry`
    (`@sentry/mcp-server`). Cada instância vira `mcp__<name>__*` no
    namespace MCP. Ver [Capabilities — templates vs instances](#capabilities--templates-vs-instances)
    pra detalhes.

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
│   ├── docker/         # Dockerfiles (agent, playwright-mcp)
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
