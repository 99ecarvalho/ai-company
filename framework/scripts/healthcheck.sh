#!/bin/bash
# Sanity check da stack: containers up, web respondendo, agentes registrados,
# transcriber, eventos pendentes.
set -uo pipefail

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$PROJECT_ROOT"

# Carrega .env pra respeitar AGENTS_DIR/COMPANY_DIR/REPOS_DIR customizados.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . .env
  set +a
fi
AGENTS_DIR="${AGENTS_DIR:-./instance/agents}"
COMPANY_DIR="${COMPANY_DIR:-./instance/company}"
REPOS_DIR="${REPOS_DIR:-./instance/repos}"

RED='\033[31m' GREEN='\033[32m' YELLOW='\033[33m' RESET='\033[0m'
pass() { echo -e "  ${GREEN}✓${RESET} $*"; }
fail() { echo -e "  ${RED}✗${RESET} $*"; }
warn() { echo -e "  ${YELLOW}!${RESET} $*"; }

# Stack base (sempre presente). Agentes vem de instance/agents/agents.yaml.
EXPECTED_BASE=(
  agent-framework-postgres-1
  agent-framework-web-1
  agent-framework-orchestrator-reactor-1
  agent-framework-scheduler-1
  agent-framework-watchdog-1
  agent-framework-transcriber-1
)

echo "==> 1. Containers (stack base)"
RUNNING=$(docker ps --format '{{.Names}}')
for c in "${EXPECTED_BASE[@]}"; do
  if echo "$RUNNING" | grep -q "^${c}$"; then pass "$c"; else fail "$c (nao rodando)"; fi
done

echo ""
echo "==> 2. Containers (agentes da instance)"
if [ -f "$AGENTS_DIR/agents.yaml" ]; then
  AGENTS=$(grep -E '^[[:space:]]+- name:' "$AGENTS_DIR/agents.yaml" | sed -E 's/.*name:[[:space:]]*//' | tr -d ' ')
  for a in $AGENTS; do
    name="agent-framework-agent-${a}-1"
    if echo "$RUNNING" | grep -q "^${name}$"; then pass "$name"; else fail "$name (nao rodando)"; fi
  done
else
  warn "$AGENTS_DIR/agents.yaml nao encontrado — pula check de agentes"
fi

echo ""
echo "==> 3. Web/Postgres health"
WSTATUS=$(curl -fsS http://localhost:9090/health 2>/dev/null || echo "")
if echo "$WSTATUS" | grep -q '"status":"ok"'; then
  VAPID=$(echo "$WSTATUS" | grep -oE '"vapid_enabled":(true|false)' | cut -d':' -f2)
  DB=$(echo "$WSTATUS" | grep -oE '"db":(true|false)' | cut -d':' -f2)
  pass "web respondendo (db=$DB, vapid_enabled=$VAPID)"
else
  fail "web nao respondeu — verifique: docker compose logs web"
fi

echo ""
echo "==> 4. Transcriber"
TSTATUS=$(docker compose exec -T transcriber curl -fsS http://localhost:8000/health 2>/dev/null || echo "")
if echo "$TSTATUS" | grep -q '"status":"ok"'; then
  MODEL=$(echo "$TSTATUS" | grep -oE '"model":"[^"]*"' | head -1 | cut -d'"' -f4)
  DEVICE=$(echo "$TSTATUS" | grep -oE '"device":"[^"]*"' | head -1 | cut -d'"' -f4)
  LOADED=$(echo "$TSTATUS" | grep -oE '"model_loaded":(true|false)' | cut -d':' -f2)
  pass "transcriber respondendo ($MODEL/$DEVICE, model_loaded=$LOADED)"
else
  fail "transcriber nao respondeu — verifique: docker compose logs transcriber"
fi

echo ""
echo "==> 5. Streams + users no broker"
STREAMS=$(curl -fsS http://localhost:9090/api/streams 2>/dev/null | grep -oE '"name":"[^"]+"' | wc -l || echo 0)
echo "  $STREAMS stream(s) registrada(s)"

echo ""
echo "==> 6. Filesystem contratos"
for p in "$AGENTS_DIR" "$COMPANY_DIR" "$REPOS_DIR" instance/heartbeats; do
  if [ -d "$p" ]; then pass "$p/"; else fail "$p/ nao existe"; fi
done

echo ""
echo "==> 7. Tasks em andamento"
# Tasks vivem em Postgres (schema `tasks`). Consulta direta — sem parse FS.
TASKS_QUERY="SELECT slug || '|' || status || '|' || COALESCE(current_step,'?') FROM tasks.tasks WHERE status NOT IN ('done','cancelled') ORDER BY updated_at DESC LIMIT 20;"
TASKS_OUT=$(docker compose exec -T postgres psql -U "${POSTGRES_USER:-agent_framework}" -d "${POSTGRES_DB:-agent_framework}" -At -c "$TASKS_QUERY" 2>/dev/null || true)
if [ -z "$TASKS_OUT" ]; then
  echo "  0 task(s) ativa(s)"
else
  echo "$TASKS_OUT" | awk -F'|' '{printf "  - %s: status=%s, current_step=%s\n", $1, $2, $3}'
  echo "  ($(echo "$TASKS_OUT" | wc -l) task(s) ativa(s))"
fi

echo ""
echo "==> healthcheck complete."
