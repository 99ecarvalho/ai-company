#!/bin/bash
# install.sh — fresh install zero-touch.
#
# Runs sanity checks, generates .env with random secrets, detects a GPU, builds
# the images, starts the stack and prints the PWA URL. Everything that used to
# be a prompt (admin password, paths, project name, port) now lives in the PWA:
#   - Settings -> System: VAPID, default stream, admin password
#   - /onboard wizard: company context + agents
#
# Multiple instances / custom paths / BYOI: edit .env *before* running
# this script. See README.md "Advanced".
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$PROJECT_ROOT"

err()  { echo "✗ $*" >&2; exit 1; }
ok()   { echo "✓ $*"; }
info() { echo "→ $*"; }

# ---------- sanity checks ----------
info "Checking prerequisites..."
command -v docker >/dev/null || err "docker not found in PATH"
docker compose version >/dev/null 2>&1 || err "'docker compose' plugin not available"
docker info >/dev/null 2>&1 || err "docker daemon not reachable (stopped? needs sudo?)"

CLAUDE_CRED="$HOME/.claude/.credentials.json"
CLAUDE_JSON="$HOME/.claude.json"
if [ ! -f "$CLAUDE_CRED" ] || [ ! -f "$CLAUDE_JSON" ]; then
  err "Claude credentials not found in ~/.claude/. Run 'claude login' on the host first."
fi
ok "docker + claude auth ok"

# ---------- submodules (external/ai-tts, external/ai-transcriber) ----------
info "Fetching submodules..."
git submodule update --init --recursive
ok "submodules ok"

# ---------- bootstrap .env + instance/ ----------
info "Bootstrapping .env + instance folders..."
bash framework/scripts/bootstrap-env.sh

# ---------- GPU auto-detect ----------
# If the host has nvidia-smi, layer docker-compose.gpu.yml on top of the main
# file — the transcriber starts with cuda + large-v3. Without a GPU, defaults CPU/small/int8.
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  if ! grep -q "^COMPOSE_FILE=" .env; then
    echo "COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml" >> .env
    ok "GPU detected — transcriber GPU layer enabled"
  else
    info "GPU detected but COMPOSE_FILE already set — keeping the user override"
  fi
else
  info "No NVIDIA GPU — transcriber runs on CPU (small/int8)"
fi

# ---------- build + up ----------
info "Building images (the first time can take a while)..."
docker compose build

info "Running reconcile (creates bots in the broker, generates override, starts stack)..."
make reconcile

# ---------- health wait ----------
WEB_PORT=$(grep -E "^WEB_PORT=" .env | cut -d= -f2 | head -1)
WEB_PORT=${WEB_PORT:-9090}
URL="http://localhost:${WEB_PORT}"

info "Waiting for ${URL}/health..."
for _ in $(seq 1 30); do
  if curl -fsS "${URL}/health" >/dev/null 2>&1; then
    ok "PWA ready at ${URL}"
    echo
    echo "  Open ${URL} in the browser — the rest happens there (/onboard wizard)."
    echo
    exit 0
  fi
  sleep 2
done

err "web did not respond at ${URL}/health after 60s — inspect 'docker compose logs web'"
