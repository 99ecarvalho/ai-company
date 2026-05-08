#!/bin/bash
# install.sh — fresh install zero-touch.
#
# Roda sanity checks, gera .env com secrets random, detecta GPU, builda
# as imagens, sobe a stack e printa a URL do PWA. Tudo o que era prompt
# antes (admin password, paths, project name, port) agora vive no PWA:
#   - Settings -> System: VAPID, default stream, admin password
#   - /onboard wizard: company context + agentes
#
# Multi-instancia / paths customizados / BYOI: edite .env *antes* de rodar
# este script. Veja README.md "Advanced".
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$PROJECT_ROOT"

err()  { echo "✗ $*" >&2; exit 1; }
ok()   { echo "✓ $*"; }
info() { echo "→ $*"; }

# ---------- sanity checks ----------
info "Checando pre-requisitos..."
command -v docker >/dev/null || err "docker nao encontrado no PATH"
docker compose version >/dev/null 2>&1 || err "plugin 'docker compose' nao disponivel"
docker info >/dev/null 2>&1 || err "docker daemon nao acessivel (parado? precisa sudo?)"

CLAUDE_CRED="$HOME/.claude/.credentials.json"
CLAUDE_JSON="$HOME/.claude.json"
if [ ! -f "$CLAUDE_CRED" ] || [ ! -f "$CLAUDE_JSON" ]; then
  err "Credenciais Claude nao encontradas em ~/.claude/. Rode 'claude login' no host antes."
fi
ok "docker + claude auth ok"

# ---------- bootstrap .env + instance/ ----------
info "Bootstrap .env + pastas de instancia..."
bash framework/scripts/bootstrap-env.sh

# ---------- GPU auto-detect ----------
# Se host tem nvidia-smi, layer docker-compose.gpu.yml por cima do principal —
# transcriber sobe com cuda + large-v3. Sem GPU, defaults CPU/small/int8.
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  if ! grep -q "^COMPOSE_FILE=" .env; then
    echo "COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml" >> .env
    ok "GPU detectada — transcriber GPU layer ativado"
  else
    info "GPU detectada mas COMPOSE_FILE ja setado — preservando override do user"
  fi
else
  info "Sem GPU NVIDIA — transcriber roda em CPU (small/int8)"
fi

# ---------- build + up ----------
info "Buildando imagens (1a vez pode demorar)..."
docker compose build

info "Aplicando reconcile (cria bots no broker, gera override, sobe stack)..."
make reconcile

# ---------- health wait ----------
WEB_PORT=$(grep -E "^WEB_PORT=" .env | cut -d= -f2 | head -1)
WEB_PORT=${WEB_PORT:-9090}
URL="http://localhost:${WEB_PORT}"

info "Aguardando ${URL}/health..."
for _ in $(seq 1 30); do
  if curl -fsS "${URL}/health" >/dev/null 2>&1; then
    ok "PWA pronto em ${URL}"
    echo
    echo "  Abra ${URL} no browser — o resto acontece la (wizard /onboard)."
    echo
    exit 0
  fi
  sleep 2
done

err "web nao respondeu em ${URL}/health apos 60s — inspecione 'docker compose logs web'"
