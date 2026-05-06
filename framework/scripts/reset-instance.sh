#!/usr/bin/env bash
# reset-instance.sh — limpa estado runtime sem tocar em config da instancia.
# Respeita AGENTS_DIR / SESSIONS_DIR do .env (D-49, D-51).
#
# Apaga:
#   - Todos os dados da DB: messages/conversations/pending_asks/memory/
#     telemetry/orchestrator events/live_events/push_subs/closed_convs.
#     Preserva schema via migrations (drop+recreate DB).
#   - ${SESSIONS_DIR}/<agent>/*: cwds por topic (session_state.json,
#     .mcp-config.json, symlinks, CLAUDE auto-memory do CLI).
#   - ${AGENTS_DIR}/<agent>/pending_questions/*
#   - instance/heartbeats/* (evita watchdog pegar heartbeat pre-reset)
#
# Preserva:
#   - ${AGENTS_DIR}/<agent>/{agent.yaml,CLAUDE.md,knowledge/}
#   - ${AGENTS_DIR}/agents.yaml
#   - ${COMPANY_DIR}/*, ${REPOS_DIR}/*, ${BACKUPS_DIR}/*
#   - .env, docker-compose.override.yml
#
# Apos reset, roda `make reconcile` pra recriar users/streams/tokens no
# broker e sobe a stack. Containers de agente sao recriados pra carregarem
# tokens novos.
#
# Uso:
#   bash framework/scripts/reset-instance.sh            # interativo
#   bash framework/scripts/reset-instance.sh --yes      # sem prompt
set -euo pipefail

YES="${1:-}"

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_ROOT"

# Carrega .env pra respeitar AGENTS_DIR customizado (D-49).
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . .env
  set +a
fi
AGENTS_DIR="${AGENTS_DIR:-./instance/agents}"
SESSIONS_DIR="${SESSIONS_DIR:-./instance/sessions}"

echo "==> reset-instance em $PROJECT_ROOT"
echo "    agents:   $AGENTS_DIR"
echo "    sessions: $SESSIONS_DIR"

if [[ "$YES" != "--yes" ]]; then
  echo
  echo "ATENCAO: isso vai APAGAR permanentemente:"
  echo "  - Toda a DB (mensagens, conversas, memoria, telemetria, events)"
  echo "  - Todas as sessions dos agentes em $SESSIONS_DIR/*/"
  echo
  read -r -p "Confirma? (digite 'sim' pra prosseguir): " CONFIRM
  if [[ "$CONFIRM" != "sim" ]]; then
    echo "cancelado."
    exit 1
  fi
fi

# --- 1. Remove containers que leem a DB (agentes/reactor/scheduler/web) ---
#
# IMPORTANTE: `docker compose rm -sf` (stop+remove), nao apenas `stop`.
# Motivo: Docker Desktop+WSL faz bind-mount de arquivo via cache por inode
# (ver QUESTIONS.md SA-A2). Quando `.claude/.credentials.json` ou `.claude.json`
# do host rotacionam (OAuth refresh), o cache do Docker fica com a ref antiga.
# Se a gente so `stop` o container, `up -d` depois tenta START (mesmo container,
# mesmo mount stale) -> erro OCI "no such file or directory" no mount source.
# `rm -sf` forca recriacao limpa no proximo up, evitando o stale.
echo "==> removendo containers (mantendo postgres up)"
docker compose rm -sf \
  web scheduler orchestrator-reactor watchdog \
  $(docker compose config --services | grep '^agent-' || true) \
  2>/dev/null || true

# --- 2. Drop + recreate DB (migrations re-aplicam schema no boot) ---
echo "==> drop + recreate do database"
docker compose up -d postgres
# Espera healthy
for i in $(seq 1 30); do
  if docker compose exec -T postgres pg_isready -U agent_framework -d agent_framework >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

docker compose exec -T postgres psql -U agent_framework -d postgres -v ON_ERROR_STOP=1 <<'SQL'
-- encerra conexoes abertas ao DB alvo
SELECT pg_terminate_backend(pid)
  FROM pg_stat_activity
 WHERE datname = 'agent_framework' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS agent_framework;
CREATE DATABASE agent_framework;
SQL

# Migrations: NAO aplicamos manualmente. O container `web` roda
# migrate.py no startup (entrypoint) e tracka em schema_migrations.
# Aplicar via psql aqui dessincroniza o tracker (tables existem mas
# schema_migrations vazia -> web re-aplica -> DuplicateTableError).

# --- 3. Apaga cwds de sessions (D-51: em ${SESSIONS_DIR}/<agent>/) ---
echo "==> limpando $SESSIONS_DIR/*/"
shopt -s nullglob
if [[ -d "$SESSIONS_DIR" ]]; then
  for agent_sessions in "$SESSIONS_DIR"/*/; do
    # remove tudo DENTRO, mas preserva $SESSIONS_DIR/<agent>/ (bind-mount)
    find "$agent_sessions" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    echo "   -> limpo: $agent_sessions"
  done
fi
# Compat: remove sessions/ legadas em $AGENTS_DIR/<agent>/sessions/ se existirem
# (instancias bootstrapadas antes de D-51; reconcile migra o mount no proximo up).
for agent_dir in "$AGENTS_DIR"/*/; do
  legacy="${agent_dir}sessions"
  if [[ -d "$legacy" ]]; then
    rm -rf "$legacy"
    echo "   -> removido legacy: $legacy (D-51)"
  fi
done

# Tambem limpa pending_questions (perguntas pendentes sao runtime state)
for agent_dir in "$AGENTS_DIR"/*/; do
  pq_dir="${agent_dir}pending_questions"
  if [[ -d "$pq_dir" ]]; then
    find "$pq_dir" -mindepth 1 -maxdepth 1 -exec rm -f {} +
  fi
done

# Heartbeats (ephemeral — zerar no reset evita watchdog pegar stale de antes)
if [[ -d instance/heartbeats ]]; then
  find instance/heartbeats -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  echo "   -> limpo: instance/heartbeats"
fi

# --- 4. Reconcile: recria users/streams/tokens no broker ---
echo "==> make reconcile (recria broker users/streams + sobe stack)"
make reconcile

# Safety net: com DB zerada, TODOS os tokens sao novos. reconcile ja
# detecta rotacao e faz --force-recreate nos agentes afetados, mas em
# caso de race/timing, faz um force-recreate explicito de todos os
# agent-* pra garantir que peguem os BROKER_TOKEN novos do .env.
echo "==> force-recreate dos containers de agente (garantia extra)"
docker compose up -d --force-recreate \
  $(docker compose config --services | grep '^agent-') \
  >/dev/null 2>&1 || true

echo
echo "==> reset completo."
echo "   DB: recreada + migrations aplicadas pelo web no startup"
echo "   Sessions: removidas"
echo "   Stack: no ar via reconcile + force-recreate"
