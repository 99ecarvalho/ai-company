#!/bin/bash
# Wrapper que executa framework/scripts/reconcile.py num container efemero com
# pyyaml+requests instalados. Depois executa `docker compose up -d` no host
# (o container efemero nao tem docker CLI pra fazer isso sozinho).
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$PROJECT_ROOT"

# Mount point precisa incluir o diretorio pai quando AGENTS_DIR/COMPANY_DIR
# apontam pra fora do manager/ (ex: ../agents). Monta o pai e usa
# PROJECT_ROOT=/work/<manager-dir> dentro do container.
PARENT_ROOT=$(dirname "$PROJECT_ROOT")
MANAGER_NAME=$(basename "$PROJECT_ROOT")

if [ ! -f .env ]; then
  echo "ERRO: .env nao encontrado. Rode framework/scripts/bootstrap-env.sh antes." >&2
  exit 1
fi

# Reconcile precisa do broker (web) pra criar users/streams. Se nao esta up,
# sobe postgres+web primeiro e espera ficar healthy.
BROKER_URL="${BROKER_URL:-http://localhost:9090}"
if ! curl -fsS "${BROKER_URL}/health" >/dev/null 2>&1; then
  echo "== Broker nao responde em ${BROKER_URL} — subindo postgres+web"
  docker compose up -d postgres web >/dev/null
  echo "== Aguardando web ficar healthy"
  tries=0
  until curl -fsS "${BROKER_URL}/health" >/dev/null 2>&1; do
    tries=$((tries+1))
    [ $tries -gt 60 ] && { echo "ERRO: web nao subiu em 120s"; exit 1; }
    sleep 2
  done
  echo "✓ web ok"
fi

# Captura stdout pra detectar se devemos rodar compose up depois
TMPOUT=$(mktemp)
trap 'rm -f "$TMPOUT"' EXIT

HOST_UID=$(id -u)
HOST_GID=$(id -g)

docker run --rm \
  --network host \
  --user "${HOST_UID}:${HOST_GID}" \
  -v "$PARENT_ROOT:/work" \
  -w "/work/$MANAGER_NAME" \
  -e HOME=/tmp \
  -e PROJECT_ROOT="/work/$MANAGER_NAME" \
  -e BROKER_URL="${BROKER_URL:-http://localhost:9090}" \
  --env-file .env \
  python:3.11-slim \
  sh -c "pip install -q --user pyyaml requests >/dev/null 2>&1 && python framework/scripts/reconcile.py $*" \
  | tee "$TMPOUT"

# Se o reconcile sinalizou pra subir, faz aqui no host
if grep -q "__RECONCILE_DO_UP__" "$TMPOUT"; then
  echo ""
  echo "== Aplicando com docker compose up -d"
  docker compose up -d
  echo "✓ stack up"

  # Se algum token foi rotacionado, recria explicitamente os containers dos
  # agentes afetados — garante que peguem o BROKER_TOKEN novo do .env. Sem
  # isso, containers criados antes da rotacao ficam com token velho e batem
  # 401 no broker.
  rotated_line=$(grep "^__RECONCILE_ROTATED__ " "$TMPOUT" || true)
  if [ -n "$rotated_line" ]; then
    rotated_names=${rotated_line#__RECONCILE_ROTATED__ }
    services=""
    for name in $rotated_names; do
      services="$services agent-$name"
    done
    echo ""
    echo "== Tokens rotacionados — force-recreate em:$services"
    docker compose up -d --force-recreate --no-deps $services
    echo "✓ agentes rotacionados recriados"
  fi
fi
