#!/bin/bash
# Backup of instance state: agents/ + company/ + docker-compose.override.yml.
# Writes a tar.gz to BACKUPS_DIR/<timestamp>.tar.gz and keeps the last N
# (default 30; via the BACKUP_RETAIN env).
#
# Usage:
#   ./framework/scripts/backup.sh              # run once
#   ./framework/scripts/backup.sh --list       # list existing backups
#   BACKUP_RETAIN=60 ./framework/scripts/backup.sh
#
# To run it automatically, use the native `backup_company` job in the PWA
# /settings/routines (cron + enable; supported by scheduler.py).
#
# Honors AGENTS_DIR/COMPANY_DIR/BACKUPS_DIR from .env (fallback instance/).
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$PROJECT_ROOT"

# Load .env to pick up AGENTS_DIR/COMPANY_DIR/BACKUPS_DIR in case the user
# customized them (multiple companies, versioned private config).
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
  ls -lh "$BACKUP_DIR" 2>/dev/null | tail -n +2 || echo "(empty)"
  exit 0
fi

mkdir -p "$BACKUP_DIR"
TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$BACKUP_DIR/backup-$TS.tar.gz"

# Paths to include. Missing ones are skipped.
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

# Retention: keep the BACKUP_RETAIN most recent, delete the rest
cd "$BACKUP_DIR"
ls -1t backup-*.tar.gz 2>/dev/null | tail -n "+$((BACKUP_RETAIN + 1))" | while read -r old; do
  rm -f "$old"
  echo "  removed (retention): $old"
done

# Print summary
TOTAL=$(ls -1 backup-*.tar.gz 2>/dev/null | wc -l | tr -d ' ')
TOTAL_SIZE=$(du -sh . 2>/dev/null | cut -f1)
echo "  total: $TOTAL backups, $TOTAL_SIZE"
