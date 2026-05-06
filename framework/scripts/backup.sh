#!/bin/bash
# Backup de estado da instancia: agents/ + company/ + docker-compose.override.yml.
# Gera tar.gz em BACKUPS_DIR/<timestamp>.tar.gz e mantem os ultimos N
# (default 30; via BACKUP_RETAIN env).
#
# Uso:
#   ./framework/scripts/backup.sh              # roda 1x
#   ./framework/scripts/backup.sh --list       # lista backups existentes
#   BACKUP_RETAIN=60 ./framework/scripts/backup.sh
#
# Pra rodar automatico, use o native job `backup_company` no PWA
# /settings/routines (cron + enable; suportado pelo scheduler.py).
#
# Respeita AGENTS_DIR/COMPANY_DIR/BACKUPS_DIR do .env (fallback instance/).
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$PROJECT_ROOT"

# Carrega .env pra pegar AGENTS_DIR/COMPANY_DIR/BACKUPS_DIR caso user os
# tenha customizado (multi-empresa, config privada versionada).
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . .env
  set +a
fi

AGENTS_DIR="${AGENTS_DIR:-./instance/agents}"
COMPANY_DIR="${COMPANY_DIR:-./instance/company}"
BACKUPS_DIR="${BACKUPS_DIR:-./instance/backups}"
BACKUP_DIR="${BACKUP_DIR:-$BACKUPS_DIR}"
BACKUP_RETAIN="${BACKUP_RETAIN:-30}"

if [ "${1:-}" = "--list" ]; then
  ls -lh "$BACKUP_DIR" 2>/dev/null | tail -n +2 || echo "(vazio)"
  exit 0
fi

mkdir -p "$BACKUP_DIR"
TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$BACKUP_DIR/backup-$TS.tar.gz"

# Paths a incluir. Se algum faltar, tar ignora silenciosamente.
INCLUDE=(
  "$COMPANY_DIR"
  "$AGENTS_DIR"
  "docker-compose.override.yml"
)
EXISTING=()
for p in "${INCLUDE[@]}"; do
  if [ -e "$p" ]; then
    EXISTING+=("$p")
  fi
done

tar -czf "$OUT" "${EXISTING[@]}" 2>/dev/null
SIZE=$(du -h "$OUT" | cut -f1)
echo "✓ backup: $OUT ($SIZE)"

# Retention: mantem os BACKUP_RETAIN mais recentes, apaga resto
cd "$BACKUP_DIR"
ls -1t backup-*.tar.gz 2>/dev/null | tail -n "+$((BACKUP_RETAIN + 1))" | while read -r old; do
  rm -f "$old"
  echo "  removido (retention): $old"
done

# Print summary
TOTAL=$(ls -1 backup-*.tar.gz 2>/dev/null | wc -l | tr -d ' ')
TOTAL_SIZE=$(du -sh . 2>/dev/null | cut -f1)
echo "  total: $TOTAL backups, $TOTAL_SIZE"
