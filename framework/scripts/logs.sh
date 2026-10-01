#!/bin/bash
# Aggregated logs for the whole stack, with pretty-printed structured JSON
# (structlog) + per-service colors.
#
# Usage:
#   bash framework/scripts/logs.sh                    # all services
#   bash framework/scripts/logs.sh agent-inbox web   # subset
#   LEVEL=error bash framework/scripts/logs.sh        # errors+warnings only
#   TAIL=200    bash framework/scripts/logs.sh        # last 200 lines
set -uo pipefail

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$PROJECT_ROOT"

TAIL="${TAIL:-50}"
LEVEL="${LEVEL:-info}"  # info | warning | error

docker compose logs -f --tail="$TAIL" --no-color "$@" 2>&1 | python3 -u -c "
import json, os, sys, re

LEVEL_RANK = {'debug': 0, 'info': 1, 'warning': 2, 'error': 3, 'critical': 4}
MIN_LEVEL = LEVEL_RANK.get(os.environ.get('LEVEL', 'info').lower(), 1)

# Color per service (cycle).
PALETTE = ['\033[36m', '\033[32m', '\033[33m', '\033[35m', '\033[34m', '\033[91m', '\033[92m', '\033[93m']
RESET = '\033[0m'
DIM = '\033[2m'
BOLD = '\033[1m'
LEVEL_COLORS = {
    'debug':    '\033[2m',
    'info':     '\033[37m',
    'warning':  '\033[33m',
    'error':    '\033[31m',
    'critical': '\033[1;31m',
}

service_colors = {}
def color_for(svc):
    if svc not in service_colors:
        service_colors[svc] = PALETTE[len(service_colors) % len(PALETTE)]
    return service_colors[svc]

# docker compose logs prefix: '<service>(-<n>)?  | <line>'
PREFIX_RE = re.compile(r'^(?P<svc>[a-zA-Z0-9_.-]+?)(?:-\d+)?\s*\|\s*(?P<rest>.*)$')

for raw in sys.stdin:
    raw = raw.rstrip()
    if not raw:
        continue
    m = PREFIX_RE.match(raw)
    if not m:
        print(raw)
        continue
    svc = m.group('svc')
    rest = m.group('rest')
    # Try parse JSON
    try:
        obj = json.loads(rest)
    except (json.JSONDecodeError, ValueError):
        # plain text — print as-is with the service color
        print(f'{color_for(svc)}{svc:18}{RESET} {rest}')
        continue
    level = (obj.get('level') or 'info').lower()
    if LEVEL_RANK.get(level, 1) < MIN_LEVEL:
        continue
    event = obj.get('event', '')
    ts = obj.get('timestamp', '')
    # Remaining kv (excluding meta)
    skip = {'event', 'level', 'timestamp', 'component'}
    extras = ' '.join(
        f'{DIM}{k}={RESET}{v}'
        for k, v in obj.items()
        if k not in skip
    )
    lvl_col = LEVEL_COLORS.get(level, '')
    short_ts = ts.split('T')[1].split('.')[0] if 'T' in ts else ts
    print(f'{DIM}{short_ts}{RESET} {color_for(svc)}{svc:18}{RESET} {lvl_col}{level:7}{RESET} {BOLD}{event}{RESET} {extras}')
"
