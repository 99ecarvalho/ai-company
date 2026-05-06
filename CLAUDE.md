# agent-framework — contexto auto-carregado

> Messaging em **broker interno** (Postgres + HTTP no container `web`).

Framework self-hosted pra orquestrar agentes Claude Code: cada agente roda em container, escuta streams (broker HTTP no `web` container, persistência Postgres com `pg_notify`), e usa MCP pra `ask_human`, `ask_agent`, `complete_phase` e memória persistente. Orchestrator (reactor LISTEN + scheduler APScheduler) + transcriber GPU (faster-whisper) + web PWA (conversas + voz + push VAPID + telemetria + file viewer/upload) acompanham o core.

> **CLAUDE.local.md** (gitignored): se existir, contém overrides locais — modo autônomo, build logs, anotações pessoais da instância. Leia-o também se presente.

## Estrutura do repo (3 zonas)

- **`framework/`** (tracked) — código do agent-framework: `bots/`, `orchestrator/`, `web/`, `transcriber/`, `watchdog/`, `docker/`, `scripts/`, `agent-template/`, `examples/`.
- **`instance/`** (gitignored) — estado/config da empresa usuária: `agents/agents.yaml`, `agents/<nome>/`, `company/`, `repos/`, `backups/`, `heartbeats/`, `sessions/<nome>/` (runtime cwds por topic, D-51). Paths customizáveis via `.env` (`AGENTS_DIR`, `COMPANY_DIR`, `BACKUPS_DIR`, `REPOS_DIR`, `SESSIONS_DIR`) — default mantém tudo em `./instance/`.
- **`.dev/`** (gitignored, opcional) — meta-desenvolvimento local: se você mantém notas em `notes/PROJECT_PLAN.md`, `notes/DECISIONS.md`, `notes/EXECUTION_LOG.md`, `notes/QUESTIONS.md`, leia antes de mexer no framework.

## Regras absolutas

- **Framework, não app fixo.** A source of truth dos agentes é `instance/agents/agents.yaml` (gitignored; inicialize via `bash framework/scripts/bootstrap-env.sh` que copia o `.example`). `docker-compose.override.yml` e `instance/agents/<name>/agent.yaml` são **gerados** por `framework/scripts/reconcile.py`. Nunca edite à mão; edite `agents.yaml` + `make reconcile`.
- **Repo = framework distribuível.** Nada específico de uma empresa ou usuário deve ir pro repo tracked. Instância inteira em `instance/`, meta-dev em `.dev/`, ambos gitignored. Defaults tracked (`framework/examples/*`, `framework/agent-template/`) têm que ser genéricos.
- **Escopo do projeto = framework + orquestrador + broker.** O conteúdo de `instance/repos/` é workspace dos agentes (código gerado pelo dev durante tasks) e **NÃO é alvo de melhoria nem revisão**.
- **Plataforma, não papéis.** Ao propor features/próximos passos, **nunca** sugerir agentes/papéis específicos de um tipo de negócio. Sugerir só capabilities de plataforma (memória, ask_agent, auto-recovery, telemetria, TLS, scaffold, plugin system, etc). Teste: "esse item é código que escrevo uma vez e serve pra qualquer empresa?" — se não, é papel, não sugere.
- **Vocabulário da instância NUNCA vai pro framework.** Nomes de fases (e.g. `intake`, `plan`, `build`, `review`, `wrap`) ou de papéis (e.g. `coordinator`, `analyst`, `executor`, `reviewer`) são convenção da instância usuária — **não são** invariantes do agent-framework. O framework só sabe de primitivas genéricas: **steps arbitrários** declarados pela instância + **terminais semânticos** que a lógica do orchestrator conhece (hoje `done`/`halt`/`human_review`, porque esses mudam status/dispatch no reactor). Sintoma do smell: ver enum/constante/mapeamento com nomes específicos de instância dentro de `framework/`. Se isso acontecer, mova a taxonomia pra config da instância (ex: `instance/company/workflows.yaml`) carregada em runtime. Teste: "se eu entregar o framework pra uma empresa que usa outros nomes de fase, o código quebra?" — se sim, é cagada.
- **Commits frequentes** conforme avança. Sem `git push` sem autorização explícita.
- **Nunca mexa fora do diretório do projeto** exceto leituras estritamente necessárias.
- **Edit Python → rebuild, não restart.** As imagens `agent` e `web` fazem `COPY` do código no build (não há bind-mount do source). `docker compose restart <svc>` reinicia o **mesmo binary** — a mudança no host **não carrega**. Fluxo correto após editar código: `docker compose build <svc> && docker compose up -d --force-recreate <svc>` (ou `make build` pra rebuildar todas). Idem após mexer em [framework/bots/agent_framework/](framework/bots/agent_framework/): rebuildar a imagem `agent` (afeta todos os containers de agente).

## Como o system prompt dos agentes eh montado

`claude_runner._build_system_prompt` ([framework/bots/agent_framework/agent_framework/claude_runner.py](framework/bots/agent_framework/agent_framework/claude_runner.py)) monta o `--append-system-prompt` concatenando, nesta ordem (cada secao tem toggle em `instance/company/system_prompts/config.yaml`):

1. `system_prompts/platform.md` — **invariantes do framework**: honestidade, hierarquia raiz->filha (broker 409), reply-as-gateway, MCP tools, baseline_sha discipline, memoria/skills/sobrecarga genericos.
2. `## Modo de invocacao` (dinamico) — query no `messaging.conversations.parent_conv_id` em runtime: diz se a conv eh raiz (humano/cron) ou filha (`ask_agent` por outro agente) + nome do pai. Substitui a heuristica "Question from `<X>`" que cada agente fazia manualmente.
3. `## Estado da task` (dinamico, condicional `topic = task-*`) — snapshot de `tasks.tasks` + `phases` + `worktrees`: slug, titulo, workflow, current_step, complexity, baselines (`metadata_extra.baseline`), worktrees ativas, fases concluidas, origin. Evita o `get_task_state` ritual no inicio da fase.
4. `## Instrucoes da fase atual` (dinamico, condicional) — campo `instructions` (markdown) do step corrente em `instance/company/workflows.yaml` -> `workflows.<wf>.steps.<step>.instructions`. **Toda mecanica de fase** (gate de aprovacao, worktree, push/MR, loop pos-review, encerramento) vive aqui — nao nos CLAUDE.md dos agentes.

   **Runtime overrides por step (D-113):** o mesmo step pode declarar `overrides: {model, effort, memory: {enabled, auto_inject_limit}}`. Quando preenchido, sobrescreve a config do agente em `agents.yaml` *apenas durante este step*. Workflow autoritario, sem teto. Util pra triagem em Sonnet/low e execucao em Opus/high sem agentes duplicados. PWA Settings -> Workflows tem fieldset dedicado por step. Hot-reload: editar via PWA aplica no proximo spawn, sem rebuild.
5. `agents/<nome>/CLAUDE.md` — **identidade + stack do papel** (PHP/Slim do core, Go repos, anti-escopo, etc). Nao deve ter mecanica de framework nem de fase — se tiver, eh sintoma de drift.
6. `company/CONTEXT.md` — catalogo geral da instancia (workflow.md, glossario.md, memoria.md, templates/, etc).
7. `company/philosophy.md` — opcional (toggle off por default).
8. `## Equipe` (dinamico) — peers que o agente pode chamar via `ask_agent` (filtrado por `agent_policies.can_ask`).

**Editor PWA:** Settings -> Workflows edita `steps.<step>.instructions` direto (textarea com block-scalar yaml `|-` no save). Settings -> Preview simula contexto via params (`?mode=child&parent=<x>&task_slug=<y>`) pra ver o concatenado final pra qualquer agente em qualquer cenario.

**Backend mirror:** `framework/web/app/main.py:_preview_*_block` reproduz a logica do runner pra preview. Se mudar uma das duas, sincronize a outra.

**Regra dura:** mecanica de framework (broker, hierarquia, MCP tools) fica em `platform.md`; mecanica de fase em `workflows.yaml.steps.<step>.instructions` (instancia, mas por workflow nao por agente); identidade de papel + stack em `agents/<nome>/CLAUDE.md`. Se um CLAUDE.md de agente comeca a falar sobre `complete_phase`/`create_worktree`/`ask_human` em filha/etc, eh sinal de que algo voltou pra camada errada.

## Mantendo `.dev/notes/` sincronizado

Se você está hackeando o framework e tem um `.dev/notes/` (PROJECT_PLAN, EXECUTION_LOG, QUESTIONS, DECISIONS), mantenha sincronizado — notas só servem se estão atualizadas. Histórico mostrou que é fácil esquecer durante rajadas de commits, e Claude futura entrando "fria" precisa desses arquivos pra ter mapa.

- **Após CADA commit ou cluster que fecha uma feature:**
  - Atualize a 1ª linha de [.dev/notes/EXECUTION_LOG.md](.dev/notes/EXECUTION_LOG.md) se o estado corrente mudou
  - Adicione 1 entrada nova no topo do EXECUTION_LOG descrevendo o que mudou
  - Decisão arquitetônica autônoma → registre em [.dev/notes/DECISIONS.md](.dev/notes/DECISIONS.md) com **D-NN** novo
  - Pergunta resolvida em QUESTIONS.md? Mova/remova.
- **Ao iniciar sessão nova:** `git log --oneline` desde último update das notes (`stat -c "%y" .dev/notes/EXECUTION_LOG.md`). Gap > 3 commits sem update → **proponha catch-up antes de começar feature nova**.
- **Antes de feature grande:** PROJECT_PLAN.md está coerente com a arquitetura atual? Se não, refresh primeiro. Commits sem notes = trabalho sem mapa pra Claude futura.
- **Critério de "atualização suficiente":** Claude novo lendo os 4 arquivos consegue (a) descrever o estado de hoje em 3 frases, (b) saber por que cada decisão recente foi tomada, (c) saber o que está aberto. Se não, falta atualizar.

Esta regra é **invariante operacional** — mesmo peso que "rode os testes antes de comitar". Em dúvida se vale atualizar, **vale**.

## Acesso rápido

- **Web PWA (interface primária):** `http://localhost:9090` — conversas, gravação voz→texto, file viewer/upload, paste de screenshots, push VAPID, telemetria 📊 (custo/tokens/latência), memória 🧠, hire de novo agente 👔.
- **Bootstrap do zero:** `bash framework/scripts/bootstrap-env.sh && make reconcile`. O bootstrap cria `.env` com secrets random + pré-cria `instance/{agents,company,repos,backups,heartbeats}` + copia `agents.yaml.example`. Reconcile detecta postgres+web não-up e sobe automaticamente. Scheduler é DB-backed (D-112): jobs custom criados via PWA `/scheduler`, native overrides via `/settings/routines`.
- **Verificar containers:** `docker compose ps` (mínimo 8: postgres, web, transcriber, watchdog, orchestrator-reactor, scheduler + N agentes).
- **Logs estruturados JSON:** `make logs-<agente>` ou `docker compose logs -f <agente>`.
- **Shell num agente:** `make shell-<agente>`.
- **Status de tasks:** `find instance/company/tasks/ -name "metadata.yaml" -exec head -5 {} \;`.
- **Eventos pendentes:** consulta no Postgres — `docker compose exec postgres psql -U agent_framework -d agent_framework -c "SELECT id, event_type, status FROM orchestrator.events WHERE status='pending';"`.
- **Novo agente:** edite `instance/agents/agents.yaml` (ou `make new-agent NAME=<slug> DISPLAY="Nome"`), depois `make reconcile` (idempotente — cuida de user+stream no broker, .env, override, compose up).
- **Transcrever áudio:** automático no PWA (clique no botão de gravação). Ad-hoc: `curl -X POST http://transcriber:8000/transcribe -F file=@audio.mp3` de dentro da rede compose.
- **Auto-recovery:** claude_runner retenta até 2x em SIGKILL/SIGTERM com `--resume`. Watchdog monitora heartbeats em `instance/heartbeats/` e restarta agent com stale > 180s (cooldown 10min). `DRY_RUN=1` no watchdog pra modo observador.
- **ask_agent (MCP tool):** agente A consulta agente B via `ask_agent(target_agent, question)`. Topic `__ask-from-A-<uid>` no `#B`. Default timeout 30min, com extensão indefinida se B estiver bloqueado em `ask_human`. Tópicos `__*` ficam ocultos do PWA exceto quando têm `pending_ask`. Habilitar adicionando `mcp__agent_framework__ask_agent` em `allowed_tools`.
- **Banco direto:** `docker compose exec postgres psql -U agent_framework -d agent_framework`. Schemas: `messaging`, `orchestrator`, `memory`, `telemetry`, `web`.

## Mexendo na PWA / frontend

- **Antes de tocar em layout/UI:** leia [framework/web/frontend/MOBILE.md](framework/web/frontend/MOBILE.md). Compila armadilhas reais (`flex-wrap` + `truncate`, `shrink-0` em group de botões, `pb-safe-nav` vs `pb-bottomNav`, cache de bundle). Custo de revisar < custo de caçar overflow horizontal num device físico.
- **Cache (D-96):** `index.html`, `sw.js`, `manifest.webmanifest` saem com `Cache-Control: no-cache`; `/_app/*` (bundles hashed) com `max-age=31536000, immutable`. Não retire esses headers — sem eles, rebuild não aparece pro user.
- **Validar mobile via Playwright:** viewport 390x844 (iPhone 14). Dump rápido:
  ```js
  await page.setViewportSize({ width: 390, height: 844 });
  page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  // > 0 = página tem overflow horizontal.
  ```
