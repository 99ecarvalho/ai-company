.DEFAULT_GOAL := help

# Load .env (if present) and export it — propagates DOCKER_CONFIG, REPOS_DIR, etc.
# to the targets that call docker/docker compose. Without this, `docker compose`
# only reads shell variables, not the project's .env.
ifneq (,$(wildcard .env))
  include .env
  export
endif

.PHONY: help up down restart logs build test shell-% logs-% reset-agent-% reset-instance reset-instance-yes reconcile new-agent healthcheck submodules submodules-latest

install: ## Interactive setup of a new instance (prompts → bootstrap → build → reconcile)
	bash framework/scripts/install.sh

help: ## List available targets
	@grep -E '^[a-zA-Z_%-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-24s %s\n", $$1, $$2}'

up: ## Start the whole stack (docker compose up -d)
	docker compose up -d

down: ## Stop the stack
	docker compose down

restart: down up ## Restart the stack

submodules: ## Fetch the external/ submodules (ai-tts, ai-transcriber) at the pinned commits
	git submodule update --init --recursive

submodules-latest: ## Move the external/ submodules to the latest commit on their main branch
	git submodule update --init --remote --recursive

build: submodules ## Build the custom images (agent + web + transcriber + watchdog)
	docker compose build

logs: ## Logs for the whole stack (tail)
	docker compose logs -f --tail=100

logs-%: ## Logs for one service (e.g. make logs-inbox)
	docker compose logs -f --tail=200 $*

logs-all: ## Aggregated logs with pretty-printed JSON + per-service colors (LEVEL=error filters)
	bash framework/scripts/logs.sh

logs-errors: ## Aggregated logs, warnings+errors only
	LEVEL=warning bash framework/scripts/logs.sh

shell-%: ## Bash in a service (e.g. make shell-inbox)
	docker compose exec $* bash

test: ## Run the framework unit tests (pytest inside the web container)
	docker compose cp framework/bots/ai_company web:/tmp/ai_company_src/
	docker compose exec -T web bash -c "cd /tmp/ai_company_src && pip install --quiet -e .[dev] && pytest tests/ -v"

test-e2e: ## Run the Playwright suite (first starts agents with CLAUDE_MOCK=1 + a large pool)
	@echo "==> enabling CLAUDE_MOCK=1 + POOL_SIZE=10 + IDLE_TIMEOUT_SEC=15 (recreate)..."
	CLAUDE_MOCK=1 POOL_SIZE=10 IDLE_TIMEOUT_SEC=15 \
	  docker compose up -d --force-recreate agent-inbox agent-executor
	@sleep 4
	@echo "==> running playwright..."
	cd framework/web/frontend && pnpm exec playwright test

test-e2e-ui: ## Playwright in interactive UI mode
	cd framework/web/frontend && pnpm exec playwright test --ui

reset-agent-%: ## Clear an agent's sessions (e.g. make reset-agent-inbox)
	@# D-51: sessions live in ${SESSIONS_DIR}/<agent>/ (outside AGENTS_DIR).
	rm -rf $(or $(SESSIONS_DIR),./instance/sessions)/$*/*
	@# Legacy: also clear $AGENTS_DIR/<agent>/sessions/ if present (pre-D-51).
	@rm -rf $(or $(AGENTS_DIR),./instance/agents)/$*/sessions 2>/dev/null || true
	@echo "Sessions of $* cleared."

tasks-migrate: ## Idempotent backfill company/tasks/ -> Postgres (D-53)
	docker compose exec -T web python3 /app/migrate_tasks_fs_to_db.py

reset-instance: ## Reset runtime state (DB + sessions), keeping config (asks for confirmation)
	bash framework/scripts/reset-instance.sh

reset-instance-yes: ## Same as reset-instance but without a prompt (dangerous — for CI/scripts)
	bash framework/scripts/reset-instance.sh --yes

healthcheck: ## Sanity check: containers up + web/db ok + transcriber + tasks
	bash framework/scripts/healthcheck.sh

reconcile: ## Apply instance/agents/agents.yaml (bots + streams + .env + override + up)
	bash framework/scripts/reconcile.sh

reconcile-dry: ## Show what reconcile would do, without applying
	bash framework/scripts/reconcile.sh --dry-run

new-agent: ## Add a new agent to agents.yaml (usage: make new-agent NAME=x DISPLAY="X")
	@[ -n "$(NAME)" ] || (echo "use: make new-agent NAME=<slug> DISPLAY=\"<Name>\""; exit 1)
	@[ -n "$(DISPLAY)" ] || (echo "use: make new-agent NAME=<slug> DISPLAY=\"<Name>\""; exit 1)
	bash framework/scripts/new-agent.sh $(NAME) "$(DISPLAY)"

migrate: ## Apply pending migrations (normally runs on its own when web boots)
	docker compose exec -T web python3 /app/migrate.py

migrate-status: ## List applied/pending migrations
	docker compose exec -T web python3 /app/migrate.py --status

migrate-baseline: ## Mark migrations as applied without running them (usage: make migrate-baseline V=1)
	@[ -n "$(V)" ] || (echo "use: make migrate-baseline V=<version>"; exit 1)
	docker compose exec -T web python3 /app/migrate.py --baseline $(V)
