#!/usr/bin/env bash
# Install, run, test and operate an ai-company stack.
# Run ./run.sh help for the commands.
set -euo pipefail

cd "$(dirname "$0")"

VENV="${VENV:-.venv}"

# Values from .env (if any) win over the defaults below, as in docker compose.
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi
PROJECT="${COMPOSE_PROJECT_NAME:-ai-company}"
WEB_PORT="${WEB_PORT:-9090}"
PG_USER="${POSTGRES_USER:-ai_company}"
PG_DB="${POSTGRES_DB:-ai_company}"
BACKUPS_DIR="${BACKUPS_DIR:-./instance/backups}"

usage() {
    cat <<EOF
Usage: ./run.sh <command> [arguments]

Install, run, test and operate the ai-company stack (project: ${PROJECT}).

Setup:
  install              First install: checks, .env, GPU detection, build,
                       reconcile and start (framework/scripts/install.sh)
  setup                Fetch submodules and create .env + instance/ without
                       building or starting anything
  submodules [--latest]
                       Fetch external/ai-tts and external/ai-transcriber at
                       the pinned commits, or move them to the newest main
  reconcile [--dry-run]
                       Apply instance/agents/agents.yaml (bots, streams,
                       override file, containers)

Run:
  start [SERVICE...]   Start the stack, or only the given services
  stop [SERVICE...]    Stop and remove the stack (volumes are kept), or
                       stop only the given services
  restart [SERVICE...] stop, then start
  build [SERVICE...]   Build images (fetches missing submodules first)
  rebuild SERVICE...   Build and recreate services. Use this after editing
                       code: the images copy the source, so a plain restart
                       keeps running the old code
  status               Container states and the health check
  logs [SERVICE...]    Follow the logs (all services by default)
  shell AGENT          Open a shell in an agent container
  psql [ARGS...]       Open psql on the stack's database

Test:
  test [SUITE]         Run tests. SUITE is one of:
                         unit      agent, reconcile and web unit tests in
                                   ${VENV} (default)
                         services  the ai-tts and ai-transcriber test suites
                         e2e       the Playwright suite against the stack
                         all       unit, then services
  smoke                Call web, TTS and transcriber on the running stack

Data:
  backup               Dump the database and archive instance/ and .env
                       into ${BACKUPS_DIR}/
  reset                Wipe runtime state (database and sessions) but keep
                       the configuration; asks for confirmation

  help                 Show this help

Environment variables (current value in brackets):
  VENV                 Virtualenv used by "test unit" [${VENV}]
  COMPOSE_PROJECT_NAME Compose project, from .env [${PROJECT}]
  WEB_PORT             Host port of the PWA, from .env [${WEB_PORT}]
  BACKUPS_DIR          Where "backup" writes, from .env [${BACKUPS_DIR}]

Examples:
  ./run.sh install
  ./run.sh rebuild web
  ./run.sh logs executor
  ./run.sh test all
  ./run.sh submodules --latest && ./run.sh rebuild tts transcriber
EOF
}

die() { echo "error: $*" >&2; exit 1; }
info() { echo "==> $*"; }

need() {
    command -v "$1" >/dev/null 2>&1 || die "$1 is required but not installed"
}

need_docker() {
    need docker
    docker compose version >/dev/null 2>&1 || die "the docker compose plugin is required"
}

cmd_install() {
    bash framework/scripts/install.sh "$@"
}

cmd_submodules() {
    need git
    if [ "${1:-}" = "--latest" ]; then
        info "Moving submodules to the newest commit on main"
        git submodule update --init --remote --recursive
        git submodule status
        echo "Commit the new submodule pointers to keep them: git add external && git commit"
    else
        info "Fetching submodules at the pinned commits"
        git submodule update --init --recursive
    fi
}

# For build/test: fetch submodules that were never checked out, but leave
# initialized ones alone, so a pointer moved with "submodules --latest" (and
# not committed yet) isn't reset to the pinned commit.
ensure_submodules() {
    need git
    if git submodule status | grep -q '^-'; then
        info "Fetching missing submodules"
        git submodule update --init --recursive
    fi
}

cmd_setup() {
    cmd_submodules
    info "Creating .env and instance/"
    bash framework/scripts/bootstrap-env.sh
}

cmd_reconcile() {
    bash framework/scripts/reconcile.sh "$@"
}

cmd_start() {
    need_docker
    if [ $# -eq 0 ] && [ ! -f docker-compose.override.yml ] && [ -f instance/agents/agents.yaml ]; then
        # No override yet: reconcile generates it, then brings everything up.
        cmd_reconcile
        return
    fi
    docker compose up -d "$@"
}

cmd_stop() {
    need_docker
    if [ $# -eq 0 ]; then
        docker compose down
    else
        docker compose stop "$@"
    fi
}

cmd_restart() {
    cmd_stop "$@"
    cmd_start "$@"
}

cmd_build() {
    need_docker
    ensure_submodules
    docker compose build "$@"
}

cmd_rebuild() {
    [ $# -gt 0 ] || die "rebuild needs at least one service (e.g. ./run.sh rebuild web)"
    cmd_build "$@"
    docker compose up -d --force-recreate "$@"
}

cmd_status() {
    need_docker
    docker compose ps
    echo
    bash framework/scripts/healthcheck.sh || true
}

cmd_logs() {
    need_docker
    local svcs=() s
    for s in "$@"; do
        # Accept agent names as well as service names: executor -> agent-executor.
        if docker compose config --services 2>/dev/null | grep -qx "agent-${s}"; then
            svcs+=("agent-${s}")
        else
            svcs+=("${s}")
        fi
    done
    docker compose logs -f --tail 200 "${svcs[@]}"
}

cmd_shell() {
    need_docker
    [ $# -eq 1 ] || die "usage: ./run.sh shell AGENT"
    docker compose exec "agent-$1" bash
}

cmd_psql() {
    need_docker
    docker compose exec postgres psql -U "${PG_USER}" -d "${PG_DB}" "$@"
}

ensure_venv() {
    need python3
    if [ ! -x "${VENV}/bin/python" ]; then
        info "Creating virtualenv ${VENV}"
        python3 -m venv "${VENV}"
    fi
    if [ ! -f "${VENV}/.deps-installed" ] \
        || [ framework/bots/ai_company/pyproject.toml -nt "${VENV}/.deps-installed" ] \
        || [ framework/web/requirements.txt -nt "${VENV}/.deps-installed" ]; then
        info "Installing test dependencies"
        "${VENV}/bin/pip" install --quiet --upgrade pip
        "${VENV}/bin/pip" install --quiet -e "framework/bots/ai_company[dev]" -r framework/web/requirements.txt
        touch "${VENV}/.deps-installed"
    fi
}

test_unit() {
    ensure_venv
    local py
    # Absolute path, but keep the venv symlink (realpath would resolve it to
    # the system interpreter, which lacks the venv's packages).
    py="$(cd "${VENV}/bin" && pwd)/python"
    info "Agent unit tests"
    (cd framework/bots/ai_company && "${py}" -m pytest -q tests)
    info "reconcile unit tests"
    (cd framework/scripts && "${py}" -m pytest -q tests)
    info "Web unit tests"
    (cd framework/web && "${py}" -m unittest discover -s app/tests -t . -q)
}

test_services() {
    ensure_submodules
    info "ai-tts tests"
    ./external/ai-tts/run.sh test
    info "ai-transcriber tests"
    if [ ! -x external/ai-transcriber/.venv/bin/python ]; then
        ./external/ai-transcriber/run.sh setup
    fi
    ./external/ai-transcriber/run.sh test
}

cmd_test() {
    case "${1:-unit}" in
        unit) test_unit ;;
        services) test_services ;;
        e2e) need make; make test-e2e ;;
        all) test_unit; test_services ;;
        *) die "unknown test suite '$1' (unit, services, e2e or all)" ;;
    esac
}

cmd_smoke() {
    need_docker
    need curl
    local url="http://127.0.0.1:${WEB_PORT}" tmp
    tmp="$(mktemp -d)"
    trap 'rm -rf "${tmp}"' RETURN

    info "web: ${url}/health"
    curl -fsS "${url}/health" && echo

    # TTS and transcriber are called inside the compose network: the web
    # proxies need a login when WEB_AUTH_DEV_BYPASS is off.
    info "tts: synthesize an MP3"
    docker compose exec -T tts curl -fsS -H 'Content-Type: application/json' \
        -d '{"text": "Hello from ai-company.", "format": "mp3"}' \
        http://127.0.0.1:8000/synthesize > "${tmp}/hello.mp3"
    echo "$(wc -c < "${tmp}/hello.mp3") bytes of MP3"

    info "transcriber: health inside the compose network"
    docker compose exec -T transcriber curl -fsS http://127.0.0.1:8000/health && echo
}

cmd_backup() {
    need_docker
    local ts dest
    ts="$(date +%Y%m%d-%H%M%S)"
    dest="${BACKUPS_DIR}/manual-${ts}"
    mkdir -p "${dest}"
    info "Dumping database ${PG_DB} to ${dest}/db.sql.gz"
    docker compose exec -T postgres pg_dump -U "${PG_USER}" -d "${PG_DB}" --no-owner --no-acl \
        | gzip > "${dest}/db.sql.gz"
    info "Archiving instance/ and .env to ${dest}/instance.tgz"
    # Earlier backups live inside instance/backups; don't nest them.
    local paths=(./instance)
    [ -f .env ] && paths+=(./.env)
    tar --exclude='./instance/backups' -czf "${dest}/instance.tgz" "${paths[@]}"
    info "Backup written to ${dest}"
    ls -la "${dest}"
}

cmd_reset() {
    bash framework/scripts/reset-instance.sh "$@"
}

main() {
    local cmd="${1:-help}"
    [ $# -gt 0 ] && shift
    case "${cmd}" in
        install) cmd_install "$@" ;;
        setup) cmd_setup ;;
        submodules) cmd_submodules "$@" ;;
        reconcile) cmd_reconcile "$@" ;;
        start|up) cmd_start "$@" ;;
        stop|down) cmd_stop "$@" ;;
        restart) cmd_restart "$@" ;;
        build) cmd_build "$@" ;;
        rebuild) cmd_rebuild "$@" ;;
        status|ps) cmd_status ;;
        logs) cmd_logs "$@" ;;
        shell) cmd_shell "$@" ;;
        psql) cmd_psql "$@" ;;
        test) cmd_test "$@" ;;
        smoke) cmd_smoke ;;
        backup) cmd_backup ;;
        reset) cmd_reset "$@" ;;
        help|-h|--help) usage ;;
        *) usage >&2; die "unknown command '${cmd}'" ;;
    esac
}

main "$@"
