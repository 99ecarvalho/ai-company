#!/bin/bash
# Stack sanity check: containers up, web responding, agents registered,
# transcriber, pending events.
set -uo pipefail

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$PROJECT_ROOT"

# Load .env to honor customized AGENTS_DIR/COMPANY_DIR/REPOS_DIR.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . .env
  set +a
fi
AGENTS_DIR="${AGENTS_DIR:-./instance/agents}"
COMPANY_DIR="${COMPANY_DIR:-./instance/company}"
REPOS_DIR="${REPOS_DIR:-./instance/repos}"
PROJECT="${COMPOSE_PROJECT_NAME:-ai-company}"
WEB_PORT="${WEB_PORT:-9090}"

RED='\033[31m' GREEN='\033[32m' YELLOW='\033[33m' RESET='\033[0m'
pass() { echo -e "  ${GREEN}✓${RESET} $*"; }
fail() { echo -e "  ${RED}✗${RESET} $*"; }
warn() { echo -e "  ${YELLOW}!${RESET} $*"; }

# Base stack (always present). Agents come from instance/agents/agents.yaml.
EXPECTED_BASE=(
  "${PROJECT}-postgres-1"
  "${PROJECT}-web-1"
  "${PROJECT}-orchestrator-reactor-1"
  "${PROJECT}-scheduler-1"
  "${PROJECT}-watchdog-1"
  "${PROJECT}-transcriber-1"
)

echo "==> 1. Containers (base stack)"
RUNNING=$(docker ps --format '{{.Names}}')
for c in "${EXPECTED_BASE[@]}"; do
  if echo "$RUNNING" | grep -q "^${c}$"; then pass "$c"; else fail "$c (not running)"; fi
done

echo ""
echo "==> 2. Containers (instance agents)"
if [ -f "$AGENTS_DIR/agents.yaml" ]; then
  AGENTS=$(grep -E '^[[:space:]]+- name:' "$AGENTS_DIR/agents.yaml" | sed -E 's/.*name:[[:space:]]*//' | tr -d ' ')
  for a in $AGENTS; do
    name="${PROJECT}-agent-${a}-1"
    if echo "$RUNNING" | grep -q "^${name}$"; then pass "$name"; else fail "$name (not running)"; fi
  done
else
  warn "$AGENTS_DIR/agents.yaml not found — skipping agent check"
fi

echo ""
echo "==> 3. Web/Postgres health"
WSTATUS=$(curl -fsS http://localhost:${WEB_PORT}/health 2>/dev/null || echo "")
if echo "$WSTATUS" | grep -q '"status":"ok"'; then
  VAPID=$(echo "$WSTATUS" | grep -oE '"vapid_enabled":(true|false)' | cut -d':' -f2)
  DB=$(echo "$WSTATUS" | grep -oE '"db":(true|false)' | cut -d':' -f2)
  pass "web responding (db=$DB, vapid_enabled=$VAPID)"
else
  fail "web did not respond — check: docker compose logs web"
fi

echo ""
echo "==> 4. Transcriber"
TSTATUS=$(docker compose exec -T transcriber curl -fsS http://localhost:8000/health 2>/dev/null || echo "")
if echo "$TSTATUS" | grep -q '"status":"ok"'; then
  MODEL=$(echo "$TSTATUS" | grep -oE '"model":"[^"]*"' | head -1 | cut -d'"' -f4)
  DEVICE=$(echo "$TSTATUS" | grep -oE '"device":"[^"]*"' | head -1 | cut -d'"' -f4)
  LOADED=$(echo "$TSTATUS" | grep -oE '"model_loaded":(true|false)' | cut -d':' -f2)
  pass "transcriber responding ($MODEL/$DEVICE, model_loaded=$LOADED)"
else
  fail "transcriber did not respond — check: docker compose logs transcriber"
fi

echo ""
echo "==> 5. Broker streams + users"
STREAMS=$(curl -fsS http://localhost:${WEB_PORT}/api/streams 2>/dev/null | grep -oE '"name":"[^"]+"' | wc -l || echo 0)
echo "  $STREAMS stream(s) registered"

echo ""
echo "==> 6. Filesystem contracts"
for p in "$AGENTS_DIR" "$COMPANY_DIR" "$REPOS_DIR" instance/heartbeats; do
  if [ -d "$p" ]; then pass "$p/"; else fail "$p/ does not exist"; fi
done

echo ""
echo "==> 7. Tasks in progress"
# Tasks live in Postgres (schema `tasks`). Direct query — no FS parsing.
TASKS_QUERY="SELECT slug || '|' || status || '|' || COALESCE(current_step,'?') FROM tasks.tasks WHERE status NOT IN ('done','cancelled') ORDER BY updated_at DESC LIMIT 20;"
TASKS_OUT=$(docker compose exec -T postgres psql -U "${POSTGRES_USER:-ai_company}" -d "${POSTGRES_DB:-ai_company}" -At -c "$TASKS_QUERY" 2>/dev/null || true)
if [ -z "$TASKS_OUT" ]; then
  echo "  0 active task(s)"
else
  echo "$TASKS_OUT" | awk -F'|' '{printf "  - %s: status=%s, current_step=%s\n", $1, $2, $3}'
  echo "  ($(echo "$TASKS_OUT" | wc -l) active task(s))"
fi

echo ""
echo "==> healthcheck complete."
