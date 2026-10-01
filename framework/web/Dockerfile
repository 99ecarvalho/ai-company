# Web app (PWA) — multi-stage:
#   stage 1: Node 20 builda o frontend SvelteKit (output em /output)
#   stage 2: Python 3.11 serve FastAPI + estaticos do build
# Build context = raiz do projeto (pra acessar framework/bots/...).

# ---------- Stage 1: build SvelteKit ----------
FROM node:20-slim AS frontend-build

ENV SVELTE_BUILD_DIR=/output \
    PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH

RUN corepack enable && corepack prepare pnpm@9 --activate

WORKDIR /build
COPY framework/web/frontend/package.json framework/web/frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY framework/web/frontend ./
RUN pnpm build && ls /output/index.html


# ---------- Stage 2: runtime FastAPI ----------
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY framework/web/requirements.txt .
RUN pip install -r requirements.txt

COPY framework/web/app ./app
COPY framework/web/static ./static
COPY framework/templates ./templates
# Framework-fixed system prompt sections (read-only; not editable per-instance).
COPY framework/system_prompts /app/system_prompts
COPY --from=frontend-build /output ./build
# Reusa MemoryStore do framework (leitura das memorias dos agentes via /memory/*.db)
COPY framework/bots/ai_company/ai_company/memory_store.py ./app/memory_store_framework.py
# Migrations + runner: aplicadas pelo entrypoint antes do uvicorn subir.
COPY framework/db/migrations ./db/migrations
COPY framework/scripts/migrate.py ./migrate.py
# Scheduler native defaults — lido por scheduler_routes.py pra montar
# /api/scheduler/routines (lista com merged override).
COPY framework/orchestrator/defaults/schedule.yaml ./framework/orchestrator/defaults/schedule.yaml
# Backfill one-shot: company/tasks/*/metadata.yaml → tasks.tasks (D-53).
# Rodar via `make tasks-migrate` apos a migration 006 — idempotente.
COPY framework/scripts/migrate_tasks_fs_to_db.py ./migrate_tasks_fs_to_db.py
# Reconcile in-process: hire/onboard rodam reconcile.py via subprocess
# dentro deste container (em vez de spawning python:3.11-slim ephemeral).
# Templates + examples ficam em /workspace/framework/... porque o reconcile
# usa PROJECT_ROOT=/workspace e procura PROJECT_ROOT/framework/{agent-template,examples}.
# /workspace eh ponto de mount: criamos a arvore aqui no build, e os mounts
# (instance/agents, instance/company, etc) sao subpastas que nao colidem.
COPY framework/scripts/ /app/scripts/
COPY framework/agent-template/ /workspace/framework/agent-template/
COPY framework/examples/ /workspace/framework/examples/
ENV MIGRATIONS_DIR=/app/db/migrations

EXPOSE 8090

# Entrypoint: aplica migrations pendentes e entao executa o CMD.
RUN printf '#!/bin/sh\nset -e\necho "[entrypoint] applying migrations"\npython3 /app/migrate.py\nexec "$@"\n' > /entrypoint.sh \
    && chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
