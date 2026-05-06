#!/bin/bash
# Gera o arquivo .env com secrets aleatorios + defaults e prepara instance/.
# Idempotente: nao sobrescreve se .env ja existe (use --force pra regenerar).
# Sempre garante as pastas de instance/ (precisam existir antes do compose up
# pra Docker nao criar bind-mounts como root).
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$PROJECT_ROOT"

# Le env vars customizaveis do ambiente primeiro, depois .env se existir
# (pra respeitar override de AGENTS_DIR/COMPANY_DIR/BACKUPS_DIR). install.sh
# exporta essas vars antes de nos chamar, entao pegamos pelo env.
read_env_var() {
  local var="$1" default="$2"
  local env_val="${!var:-}"
  if [ -n "$env_val" ]; then echo "$env_val"; return; fi
  if [ -f .env ]; then
    local v
    v=$(grep -E "^${var}=" .env | head -1 | cut -d= -f2- || true)
    if [ -n "$v" ]; then echo "$v"; return; fi
  fi
  echo "$default"
}

AGENTS_DIR_HOST=$(read_env_var AGENTS_DIR "./instance/agents")
COMPANY_DIR_HOST=$(read_env_var COMPANY_DIR "./instance/company")
BACKUPS_DIR_HOST=$(read_env_var BACKUPS_DIR "./instance/backups")
REPOS_DIR_HOST=$(read_env_var REPOS_DIR "./instance/repos")
WORKTREES_DIR_HOST=$(read_env_var WORKTREES_DIR "./instance/worktrees")
SESSIONS_DIR_HOST=$(read_env_var SESSIONS_DIR "./instance/sessions")

# Garante tudo que agent/web/orchestrator fazem bind-mount — impede Docker
# de criar essas pastas como root na primeira subida.
echo "→ Preparando pastas da instancia"
mkdir -p \
  "$AGENTS_DIR_HOST" \
  "$COMPANY_DIR_HOST" \
  "$REPOS_DIR_HOST" \
  "$WORKTREES_DIR_HOST" \
  "$BACKUPS_DIR_HOST" \
  "$SESSIONS_DIR_HOST" \
  instance/heartbeats

# Worktrees precisa ser escrita por agente (UID node=1000) e tambem pelo host
# (UID do usuario dev). Bind-mount herda ownership do host — abrimos permissao
# pra que o agente consiga criar e o host consiga inspecionar/limpar.
chmod 777 "$WORKTREES_DIR_HOST" 2>/dev/null || true

# Seed do CONTEXT.md (contexto compartilhado injetado no system prompt
# de todos os agentes).
if [ ! -e "$COMPANY_DIR_HOST/CONTEXT.md" ]; then
  if [ -f framework/templates/CONTEXT.md.example ]; then
    cp framework/templates/CONTEXT.md.example "$COMPANY_DIR_HOST/CONTEXT.md"
    echo "  seed: $COMPANY_DIR_HOST/CONTEXT.md copiado do template (edite no PWA)"
  fi
fi

# Seed do philosophy.md (filosofia operacional ativa). Vazio por default;
# preenchido pelo wizard de onboarding ou cp manual de framework/templates/philosophies/.
if [ ! -e "$COMPANY_DIR_HOST/philosophy.md" ]; then
  printf '# Philosophy\n\n_(opcional — define modelo operacional. Veja templates em framework/templates/philosophies/.)_\n' \
    > "$COMPANY_DIR_HOST/philosophy.md"
fi

# Seed do agents.yaml se nao existir.
if [ ! -e "$AGENTS_DIR_HOST/agents.yaml" ]; then
  if [ -f framework/examples/agents.yaml.example ]; then
    cp framework/examples/agents.yaml.example "$AGENTS_DIR_HOST/agents.yaml"
    echo "  seed: $AGENTS_DIR_HOST/agents.yaml copiado do example (edite e rode make reconcile)"
  fi
fi

# Seed do workflows.yaml se nao existir. Opcional — na ausencia, o framework
# opera em modo permissivo (aceita qualquer step, exige next_agent explicito).
if [ ! -e "$COMPANY_DIR_HOST/workflows.yaml" ]; then
  if [ -f framework/examples/workflows.yaml.example ]; then
    cp framework/examples/workflows.yaml.example "$COMPANY_DIR_HOST/workflows.yaml"
    echo "  seed: $COMPANY_DIR_HOST/workflows.yaml copiado do example (edite pra declarar seus steps)"
  fi
fi

ENV_FILE=".env"
FORCE=${1:-}

if [ -f "$ENV_FILE" ] && [ "$FORCE" != "--force" ]; then
  echo "$ENV_FILE ja existe. Use '--force' pra regenerar."
  exit 0
fi

gen() { openssl rand -hex 32; }

cat > "$ENV_FILE" <<EOF
# Gerado por framework/scripts/bootstrap-env.sh em $(date -u +%Y-%m-%dT%H:%M:%SZ)
# NAO COMITAR este arquivo. .gitignore ja ignora.

# --- Postgres ---
POSTGRES_DB=agent_framework
POSTGRES_USER=agent_framework
POSTGRES_PASSWORD=$(gen)

# --- Admin ---
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=

# --- PWA auth ---
# DEV: 1 mantem admin implicito quando nao ha cookie/token (single-user local).
# PROD: deixe vazio + ADMIN_PASSWORD setado pra forcar login.
WEB_AUTH_DEV_BYPASS=1
# Marcar cookies como Secure (=requer HTTPS). Setar 1 quando expor com TLS.
WEB_COOKIE_SECURE=

# --- Service tokens ---
ORCHESTRATOR_TOKEN=$(gen)
SCHEDULER_TOKEN=$(gen)

# --- VAPID (gere depois com framework/scripts/generate-vapid.py se for usar push) ---
VAPID_PUBLIC_KEY=
VAPID_PRIVATE_KEY=

# --- Transcriber (opcional; ajuste conforme GPU/CPU) ---
WHISPER_MODEL=large-v3
WHISPER_DEVICE=cuda
WHISPER_COMPUTE_TYPE=float16
WHISPER_LANGUAGE=

# --- Timezone ---
TZ=America/Sao_Paulo

# --- Deploy: multi-empresa na mesma maquina ---
# Para rodar varias empresas lado a lado, copie o framework pra pastas separadas
# e troque COMPOSE_PROJECT_NAME + WEB_PORT em cada .env.
COMPOSE_PROJECT_NAME=company-agents
WEB_PORT=9090

# --- BYOI / Docker config (registry privado) ---
# Preencha se algum agente usar image: de registry privado.
# Setup: DOCKER_CONFIG=./.docker docker login <registry> (install.sh faz auto).
DOCKER_CONFIG=

# --- Paths customizaveis da instancia ---
# Bind-mounts que o docker-compose expande no host. Defaults mantem tudo
# em ./instance/ (comportamento classico). Troque se quiser:
#  - versionar config separada (AGENTS_DIR/COMPANY_DIR em repo proprio),
#  - rodar multi-empresa com 1 framework + N instancias em paths distintos,
#  - pipar backups pra disco/mount diferente,
#  - manter repos FORA do framework pra editor nao ver gits aninhados.
AGENTS_DIR=./instance/agents
COMPANY_DIR=./instance/company
BACKUPS_DIR=./instance/backups
REPOS_DIR=./instance/repos
# Worktrees vao aqui (fora de REPOS_DIR) pra nao poluir status do repo
# canonico. create_worktree cria em WORKTREES_DIR/<repo>/<slug>/.
WORKTREES_DIR=./instance/worktrees
# Runtime cwds por topic (D-51). Separado de AGENTS_DIR pra config persistente
# (agent.yaml, CLAUDE.md, knowledge/) nao misturar com state efemero (GC 24h).
SESSIONS_DIR=./instance/sessions

# --- Capability mysql-producao (opcional) ---
# Credenciais read-only de producao consumidas pelo MCP MySQL lateral
# (framework/docker/mysql-mcp.Dockerfile). Preencha apenas se algum agente
# declarar \`capabilities: [mysql-producao]\` em agents.yaml. Se vazio, o
# container sobe mas nao conecta — agente fica em fallback.
DB_PROD_HOST=
DB_PROD_PORT=3306
DB_PROD_USER=
DB_PROD_PASS=
DB_PROD_NAME=

# --- Git push / MR-PR (usados por executores e revisor-testador) ---
# Injetadas em todos os agentes pelo reconcile. Imagem do agente traz
# glab (GitLab) e gh (GitHub) pre-instalados — framework-agnostico.
# GITLAB_TOKEN: scope api + write_repository
# GITLAB_HOST: so se self-hosted (ex: gitlab.empresa.com)
# GH_TOKEN: scope repo
GITLAB_TOKEN=
GITLAB_HOST=
GH_TOKEN=
GIT_AUTHOR_NAME=agent-framework
GIT_AUTHOR_EMAIL=agents@local

# Proteção anti-push-main: hooks_defaults em agents.yaml (D-60) — PreToolUse
# do Claude Code bloqueia agentes tentando push em main/master. Push do host
# e livre. Script generico em framework/examples/hooks/block-push-main.sh —
# copie pra \${HOOKS_DIR:-instance/hooks}/ e referencie em agents.yaml.

# --- Test mode (vazio em producao, "1" pra mockar Claude nos testes) ---
CLAUDE_MOCK=
CLAUDE_MOCK_REPLY=
EOF

chmod 600 "$ENV_FILE"
echo "$ENV_FILE criado com permissao 600."
echo "Conteudo (secrets ocultos):"
sed -E 's/(=[a-f0-9]{8})[a-f0-9]+/\1.../' "$ENV_FILE"
