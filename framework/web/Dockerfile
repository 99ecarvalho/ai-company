# Web app (PWA) — multi-stage:
#   stage 1: Node 20 builds the SvelteKit frontend (output in /output)
#   stage 2: Python 3.11 serves FastAPI + the build's static files
# Build context = project root (to reach framework/bots/...).

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
# Reuses the framework's MemoryStore (reads agent memories via /memory/*.db)
COPY framework/bots/ai_company/ai_company/memory_store.py ./app/memory_store_framework.py
# Migrations + runner: applied by the entrypoint before uvicorn starts.
COPY framework/db/migrations ./db/migrations
COPY framework/scripts/migrate.py ./migrate.py
# Scheduler native defaults — read by scheduler_routes.py to build
# /api/scheduler/routines (list with merged override).
COPY framework/orchestrator/defaults/schedule.yaml ./framework/orchestrator/defaults/schedule.yaml
# Backfill one-shot: company/tasks/*/metadata.yaml → tasks.tasks (D-53).
# Run via `make tasks-migrate` after migration 006 — idempotent.
COPY framework/scripts/migrate_tasks_fs_to_db.py ./migrate_tasks_fs_to_db.py
# In-process reconcile: hire/onboard run reconcile.py via subprocess
# inside this container (instead of spawning an ephemeral python:3.11-slim).
# Templates + examples live in /workspace/framework/... because reconcile
# uses PROJECT_ROOT=/workspace and looks for PROJECT_ROOT/framework/{agent-template,examples}.
# /workspace is a mount point: we create the tree here at build time, and the mounts
# (instance/agents, instance/company, etc) are subfolders that don't collide.
COPY framework/scripts/ /app/scripts/
COPY framework/agent-template/ /workspace/framework/agent-template/
COPY framework/examples/ /workspace/framework/examples/
ENV MIGRATIONS_DIR=/app/db/migrations

EXPOSE 8090

# Entrypoint: applies pending migrations, then runs the CMD.
RUN printf '#!/bin/sh\nset -e\necho "[entrypoint] applying migrations"\npython3 /app/migrate.py\nexec "$@"\n' > /entrypoint.sh \
    && chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
