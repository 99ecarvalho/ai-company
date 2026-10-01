#!/usr/bin/env bash
# reset-instance.sh — clears runtime state without touching instance config.
# Honors AGENTS_DIR / SESSIONS_DIR from .env (D-49, D-51).
#
# Deletes:
#   - All DB data: messages/conversations/pending_asks/memory/
#     telemetry/orchestrator events/live_events/push_subs/closed_convs.
#     The schema is restored by the migrations (drop+recreate DB).
#   - ${SESSIONS_DIR}/<agent>/*: per-topic cwds (session_state.json,
#     .mcp-config.json, symlinks, the CLI's CLAUDE auto-memory).
#   - ${AGENTS_DIR}/<agent>/pending_questions/*
#   - instance/heartbeats/* (keeps the watchdog from reading pre-reset heartbeats)
#
# Keeps:
#   - ${AGENTS_DIR}/<agent>/{agent.yaml,CLAUDE.md,knowledge/}
#   - ${AGENTS_DIR}/agents.yaml
#   - ${COMPANY_DIR}/*, ${REPOS_DIR}/*, ${BACKUPS_DIR}/*
#   - .env, docker-compose.override.yml
#
# After the reset, runs `make reconcile` to recreate users/streams/tokens in the
# broker and starts the stack. Agent containers are recreated so they load
# the new tokens.
#
# Usage:
#   bash framework/scripts/reset-instance.sh            # interactive
#   bash framework/scripts/reset-instance.sh --yes      # no prompt
set -euo pipefail

YES="${1:-}"

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_ROOT"

# Load .env to honor a customized AGENTS_DIR (D-49).
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . .env
  set +a
fi
PG_USER="${POSTGRES_USER:-ai_company}"
PG_DB="${POSTGRES_DB:-ai_company}"
AGENTS_DIR="${AGENTS_DIR:-./instance/agents}"
SESSIONS_DIR="${SESSIONS_DIR:-./instance/sessions}"

echo "==> reset-instance in $PROJECT_ROOT"
echo "    agents:   $AGENTS_DIR"
echo "    sessions: $SESSIONS_DIR"

if [[ "$YES" != "--yes" ]]; then
  echo
  echo "WARNING: this will PERMANENTLY DELETE:"
  echo "  - The whole DB (messages, conversations, memory, telemetry, events)"
  echo "  - All agent sessions in $SESSIONS_DIR/*/"
  echo
  read -r -p "Confirm? (type 'yes' to proceed): " CONFIRM
  if [[ "$CONFIRM" != "yes" ]]; then
    echo "cancelled."
    exit 1
  fi
fi

# --- 1. Remove the containers that read the DB (agents/reactor/scheduler/web) ---
#
# IMPORTANT: `docker compose rm -sf` (stop+remove), not just `stop`.
# Reason: Docker Desktop+WSL bind-mounts files through a per-inode cache
# (see QUESTIONS.md SA-A2). When the host's `.claude/.credentials.json` or
# `.claude.json` rotates (OAuth refresh), Docker's cache keeps the old ref.
# If we only `stop` the container, a later `up -d` tries START (same container,
# same stale mount) -> OCI error "no such file or directory" on the mount source.
# `rm -sf` forces a clean recreate on the next up, avoiding the stale mount.
echo "==> removing containers (keeping postgres up)"
docker compose rm -sf \
  web scheduler orchestrator-reactor watchdog \
  $(docker compose config --services | grep '^agent-' || true) \
  2>/dev/null || true

# --- 2. Drop + recreate DB (migrations re-apply the schema at boot) ---
echo "==> drop + recreate database"
docker compose up -d postgres
# Wait until healthy
for i in $(seq 1 30); do
  if docker compose exec -T postgres pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

docker compose exec -T postgres psql -U "$PG_USER" -d postgres -v ON_ERROR_STOP=1 -v db="$PG_DB" <<'SQL'
-- terminate open connections to the target DB
SELECT pg_terminate_backend(pid)
  FROM pg_stat_activity
 WHERE datname = :'db' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS :"db";
CREATE DATABASE :"db";
SQL

# Migrations: we do NOT apply them by hand. The `web` container runs
# migrate.py at startup (entrypoint) and tracks them in schema_migrations.
# Applying via psql here desyncs the tracker (tables exist but
# schema_migrations is empty -> web re-applies -> DuplicateTableError).

# --- 3. Delete session cwds (D-51: in ${SESSIONS_DIR}/<agent>/) ---
echo "==> clearing $SESSIONS_DIR/*/"
shopt -s nullglob
if [[ -d "$SESSIONS_DIR" ]]; then
  for agent_sessions in "$SESSIONS_DIR"/*/; do
    # remove everything INSIDE, but keep $SESSIONS_DIR/<agent>/ (bind mount)
    find "$agent_sessions" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    echo "   -> cleared: $agent_sessions"
  done
fi

# Also clear pending_questions (pending questions are runtime state)
for agent_dir in "$AGENTS_DIR"/*/; do
  pq_dir="${agent_dir}pending_questions"
  if [[ -d "$pq_dir" ]]; then
    find "$pq_dir" -mindepth 1 -maxdepth 1 -exec rm -f {} +
  fi
done

# Heartbeats (ephemeral — clearing them on reset keeps the watchdog from seeing stale ones)
if [[ -d instance/heartbeats ]]; then
  find instance/heartbeats -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  echo "   -> cleared: instance/heartbeats"
fi

# --- 4. Reconcile: recreate users/streams/tokens in the broker ---
echo "==> make reconcile (recreates broker users/streams + starts stack)"
make reconcile

# Safety net: with an empty DB, ALL tokens are new. reconcile already
# detects rotation and runs --force-recreate on the affected agents, but in
# case of a race/timing issue, explicitly force-recreate every agent-*
# to make sure they pick up the new BROKER_TOKENs from .env.
echo "==> force-recreate agent containers (extra safety)"
docker compose up -d --force-recreate \
  $(docker compose config --services | grep '^agent-') \
  >/dev/null 2>&1 || true

echo
echo "==> reset complete."
echo "   DB: recreated + migrations applied by web at startup"
echo "   Sessions: removed"
echo "   Stack: up via reconcile + force-recreate"
