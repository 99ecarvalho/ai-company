#!/bin/bash
# install.sh — setup interativo de uma nova instancia do agent-framework.
#
# Uso:
#   bash framework/scripts/install.sh
#   # ou, pelo Makefile:
#   make install
#
# Idempotente: rodar 2x nao quebra. Builda imagens localmente (nao depende
# de registry publicado). Pra multi-empresa: copie o repo pra uma pasta
# nova, rode install.sh la com COMPOSE_PROJECT_NAME e WEB_PORT distintos.
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$PROJECT_ROOT"

# ---------- helpers ----------
err()  { echo "✗ $*" >&2; exit 1; }
ok()   { echo "✓ $*"; }
info() { echo "→ $*"; }
warn() { echo "⚠ $*"; }

prompt() {
  # prompt VAR_NAME DEFAULT MESSAGE
  local var="$1" default="${2:-}" msg="$3" input
  if [ -n "$default" ]; then
    read -rp "  $msg [$default]: " input
    printf -v "$var" "%s" "${input:-$default}"
  else
    read -rp "  $msg: " input
    printf -v "$var" "%s" "$input"
  fi
}

prompt_secret() {
  # prompt_secret VAR_NAME MESSAGE
  local var="$1" msg="$2" input
  read -rsp "  $msg: " input; echo
  printf -v "$var" "%s" "$input"
}

update_env() {
  # update_env KEY VALUE — upsert em .env (sem expandir vars; valor literal)
  local key="$1" val="$2" tmp
  tmp=$(mktemp)
  if grep -q "^${key}=" .env 2>/dev/null; then
    awk -v k="$key" -v v="$val" 'BEGIN{FS=OFS="="} $1==k{print k"="v; next} {print}' .env > "$tmp"
    mv "$tmp" .env
  else
    rm -f "$tmp"
    echo "${key}=${val}" >> .env
  fi
}

slugify() {
  echo "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g'
}

# ---------- sanity checks ----------
echo
info "Checando pre-requisitos..."
command -v docker >/dev/null || err "docker nao encontrado no PATH"
docker compose version >/dev/null 2>&1 || err "plugin 'docker compose' nao disponivel"
docker info >/dev/null 2>&1 || err "docker daemon nao acessivel (esta parado? precisa sudo?)"

CLAUDE_CRED="$HOME/.claude/.credentials.json"
CLAUDE_JSON="$HOME/.claude.json"
if [ ! -f "$CLAUDE_CRED" ] || [ ! -f "$CLAUDE_JSON" ]; then
  err "Credenciais Claude nao encontradas em ~/.claude/. Rode 'claude login' no host antes de instalar."
fi
ok "docker ok + claude auth presente"

# ---------- coleta de config ----------
echo
echo "== Configuracao da instancia =="
echo

prompt COMPANY_NAME "" "Nome da empresa (texto livre, usado no display)"
[ -z "$COMPANY_NAME" ] && err "nome da empresa nao pode ser vazio"

SLUG=$(slugify "$COMPANY_NAME")
[ -z "$SLUG" ] && SLUG="company"

prompt COMPOSE_PROJECT_NAME "company-agents-$SLUG" "COMPOSE_PROJECT_NAME (namespaceia containers/volumes)"

prompt WEB_PORT "9090" "Porta do PWA no host"
if command -v ss >/dev/null && ss -tln 2>/dev/null | grep -q ":${WEB_PORT} "; then
  warn "porta $WEB_PORT ja parece estar em uso neste host"
  prompt WEB_PORT "" "Escolha outra porta"
  [ -z "$WEB_PORT" ] && err "porta vazia"
fi

prompt REPOS_DIR "./instance/repos" "Path dos repos dos agentes (../gitlab recomendado se quiser ficar fora do framework)"

# AGENTS_DIR / COMPANY_DIR / BACKUPS_DIR: avançado — default cobre a maioria.
# Prompt só se o usuario pedir customizacao; senao usa instance/*.
echo
read -rp "  Customizar paths de agents/company/backups (multi-empresa, config versionada privada)? [y/N]: " CUSTOM_PATHS
if [[ "${CUSTOM_PATHS:-n}" =~ ^[yY] ]]; then
  prompt AGENTS_DIR "./instance/agents" "Path da config dos agentes (agents.yaml + <name>/)"
  prompt COMPANY_DIR "./instance/company" "Path do contexto da empresa (CONTEXT.md, workflow.md, tasks/, backlog/, ...)"
  prompt BACKUPS_DIR "./instance/backups" "Path dos backups gerados pelo scheduler"
else
  AGENTS_DIR="./instance/agents"
  COMPANY_DIR="./instance/company"
  BACKUPS_DIR="./instance/backups"
fi

prompt ADMIN_EMAIL "admin@${SLUG}.local" "Email do admin"
prompt_secret ADMIN_PASSWORD "Senha admin (vazio = manter dev bypass sem login)"
if [ -n "$ADMIN_PASSWORD" ]; then
  prompt_secret ADMIN_PASSWORD2 "Confirme a senha"
  [ "$ADMIN_PASSWORD" = "$ADMIN_PASSWORD2" ] || err "senhas nao batem"
fi

# GPU detection
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  DEFAULT_DEVICE="cuda"
  GPU_AVAILABLE=1
else
  DEFAULT_DEVICE="cpu"
  GPU_AVAILABLE=0
  warn "nvidia-smi indisponivel — transcriber vai rodar em CPU (mais lento; usamos modelo menor)"
fi
prompt WHISPER_DEVICE "$DEFAULT_DEVICE" "Device do Whisper (cuda|cpu)"

# Registry privado (BYOI) — DOCKER_CONFIG isolado por empresa evita
# colisao de credenciais com outras contas do mesmo registry.
echo
read -rp "  Algum agente usa imagem de registry privado (BYOI)? [y/N]: " USE_PRIVATE_REG
USE_DOCKER_CONFIG=0
if [[ "${USE_PRIVATE_REG:-n}" =~ ^[yY] ]]; then
  USE_DOCKER_CONFIG=1
  prompt REGISTRY_URL "registry.gitlab.com" "Registry URL (ex: registry.gitlab.com, ghcr.io)"
fi

echo
info "Resumo:"
echo "    empresa:             $COMPANY_NAME"
echo "    project:             $COMPOSE_PROJECT_NAME"
echo "    porta:               $WEB_PORT"
echo "    agents:              $AGENTS_DIR"
echo "    company:             $COMPANY_DIR"
echo "    repos:               $REPOS_DIR"
echo "    backups:             $BACKUPS_DIR"
echo "    admin:               $ADMIN_EMAIL ($([ -n "$ADMIN_PASSWORD" ] && echo "com senha" || echo "dev bypass"))"
echo "    whisper:             $WHISPER_DEVICE"
echo
read -rp "Confirma? [Y/n]: " CONFIRM
case "${CONFIRM:-Y}" in
  [nN]*) err "cancelado" ;;
esac

# ---------- bootstrap .env + pastas da instancia ----------
# Exporta paths pro bootstrap-env.sh criar as pastas nos lugares certos
# (nao apenas no default instance/).
export AGENTS_DIR COMPANY_DIR BACKUPS_DIR REPOS_DIR
echo
info "Executando bootstrap-env (gera .env com secrets + prepara pastas)..."
bash framework/scripts/bootstrap-env.sh

info "Ajustando .env com valores escolhidos..."
update_env COMPOSE_PROJECT_NAME "$COMPOSE_PROJECT_NAME"
update_env WEB_PORT "$WEB_PORT"
update_env AGENTS_DIR "$AGENTS_DIR"
update_env COMPANY_DIR "$COMPANY_DIR"
update_env BACKUPS_DIR "$BACKUPS_DIR"
update_env REPOS_DIR "$REPOS_DIR"
update_env ADMIN_EMAIL "$ADMIN_EMAIL"
update_env ADMIN_PASSWORD "$ADMIN_PASSWORD"
update_env WHISPER_DEVICE "$WHISPER_DEVICE"
if [ -z "$ADMIN_PASSWORD" ]; then
  update_env WEB_AUTH_DEV_BYPASS "1"
else
  update_env WEB_AUTH_DEV_BYPASS ""
fi
if [ "$WHISPER_DEVICE" = "cpu" ]; then
  update_env WHISPER_MODEL "base"
  update_env WHISPER_COMPUTE_TYPE "int8"
fi

# DOCKER_CONFIG por empresa (evita colisao com outras contas no mesmo registry)
if [ "$USE_DOCKER_CONFIG" -eq 1 ]; then
  mkdir -p "$PROJECT_ROOT/.docker"
  chmod 700 "$PROJECT_ROOT/.docker"
  echo
  info "Fazendo docker login em ${REGISTRY_URL} (credenciais vao pra ./.docker/, isoladas desta empresa)..."
  DOCKER_CONFIG="$PROJECT_ROOT/.docker" docker login "$REGISTRY_URL" || err "docker login falhou"
  update_env DOCKER_CONFIG "./.docker"
  ok "docker login salvo em ./.docker/config.json"
fi
ok ".env pronto"

# ---------- seed CONTEXT.md com nome da empresa ----------
if [ -f "$COMPANY_DIR/CONTEXT.md" ]; then
  sed -i.bak "1s|.*|# ${COMPANY_NAME}|" "$COMPANY_DIR/CONTEXT.md" && rm -f "$COMPANY_DIR/CONTEXT.md.bak"
fi

# ---------- build imagens ----------
echo
info "Buildando imagens locais (pode levar alguns minutos na primeira vez)..."
docker compose build

# ---------- reconcile + up ----------
echo
info "Aplicando reconcile (cria bots no broker, gera override, sobe stack)..."
make reconcile

# ---------- health wait ----------
echo
info "Aguardando /health ficar ok..."
HEALTH_URL="http://localhost:${WEB_PORT}/health"
READY=0
for _ in $(seq 1 30); do
  if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 2
done

if [ "$READY" -ne 1 ]; then
  err "web nao respondeu em $HEALTH_URL apos 60s — inspecione 'docker compose logs web'"
fi

# ---------- summary ----------
echo
ok "Instalacao completa!"
echo
echo "  PWA:     http://localhost:${WEB_PORT}"
echo "  Admin:   ${ADMIN_EMAIL}"
if [ -z "$ADMIN_PASSWORD" ]; then
  echo "  Auth:    dev bypass (sem login)"
else
  echo "  Auth:    login necessario (senha definida)"
fi
echo
echo "Proximos passos:"
echo "  - abra o PWA e complete o onboarding wizard"
echo "  - 'make logs-all' pra acompanhar logs"
echo "  - 'make reconcile' depois de editar instance/agents/agents.yaml"
[ "$GPU_AVAILABLE" -eq 0 ] && echo "  - sem GPU: qualidade do Whisper reduzida (modelo 'base')"
echo
