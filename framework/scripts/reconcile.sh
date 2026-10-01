#!/bin/bash
# Wrapper that runs framework/scripts/reconcile.py in an ephemeral container with
# pyyaml+requests installed. Then runs `docker compose up -d` on the host
# (the ephemeral container has no docker CLI to do it itself).
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$PROJECT_ROOT"

# The mount point must include the parent dir when AGENTS_DIR/COMPANY_DIR
# point outside manager/ (e.g. ../agents). Mount the parent and use
# PROJECT_ROOT=/work/<manager-dir> inside the container.
PARENT_ROOT=$(dirname "$PROJECT_ROOT")
MANAGER_NAME=$(basename "$PROJECT_ROOT")

if [ ! -f .env ]; then
  echo "ERROR: .env not found. Run framework/scripts/bootstrap-env.sh first." >&2
  exit 1
fi

# Reconcile needs the broker (web) to create users/streams. If it is not up,
# start postgres+web first and wait until healthy.
BROKER_URL="${BROKER_URL:-http://localhost:9090}"
if ! curl -fsS "${BROKER_URL}/health" >/dev/null 2>&1; then
  echo "== Broker not responding at ${BROKER_URL} — starting postgres+web"
  docker compose up -d postgres web >/dev/null
  echo "== Waiting for web to become healthy"
  tries=0
  until curl -fsS "${BROKER_URL}/health" >/dev/null 2>&1; do
    tries=$((tries+1))
    [ $tries -gt 60 ] && { echo "ERROR: web did not come up within 120s"; exit 1; }
    sleep 2
  done
  echo "✓ web ok"
fi

# Capture stdout to detect whether we should run compose up afterwards
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

# If reconcile signaled to bring things up, do it here on the host
if grep -q "__RECONCILE_DO_UP__" "$TMPOUT"; then
  echo ""
  echo "== Applying with docker compose up -d"
  docker compose up -d
  echo "✓ stack up"

  # If any token was rotated, explicitly recreate the affected agents'
  # containers — ensures they pick up the new BROKER_TOKEN from .env. Without
  # this, containers created before the rotation keep the old token and get
  # 401 from the broker.
  rotated_line=$(grep "^__RECONCILE_ROTATED__ " "$TMPOUT" || true)
  if [ -n "$rotated_line" ]; then
    rotated_names=${rotated_line#__RECONCILE_ROTATED__ }
    services=""
    for name in $rotated_names; do
      services="$services agent-$name"
    done
    echo ""
    echo "== Tokens rotated — force-recreate on:$services"
    docker compose up -d --force-recreate --no-deps $services
    echo "✓ rotated agents recreated"
  fi
fi
