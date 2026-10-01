.DEFAULT_GOAL := help

# Carrega .env (se existir) e exporta — propaga DOCKER_CONFIG, REPOS_DIR, etc.
# pros targets que chamam docker/docker compose. Sem isso, `docker compose`
# le so variaveis do shell, nao o .env do projeto.
ifneq (,$(wildcard .env))
  include .env
  export
endif

.PHONY: help up down restart logs build test shell-% logs-% reset-agent-% reset-instance reset-instance-yes reconcile new-agent healthcheck submodules submodules-latest

install: ## Setup interativo de uma nova instância (prompts → bootstrap → build → reconcile)
	bash framework/scripts/install.sh

help: ## Lista targets disponíveis
	@grep -E '^[a-zA-Z_%-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-24s %s\n", $$1, $$2}'

up: ## Sobe a stack inteira (docker compose up -d)
	docker compose up -d

down: ## Derruba a stack
	docker compose down

restart: down up ## Reinicia a stack

submodules: ## Fetch the external/ submodules (ai-tts, ai-transcriber) at the pinned commits
	git submodule update --init --recursive

submodules-latest: ## Move the external/ submodules to the latest commit on their main branch
	git submodule update --init --remote --recursive

build: submodules ## Build das imagens customizadas (agent + web + transcriber + watchdog)
	docker compose build

logs: ## Logs de toda a stack (tail)
	docker compose logs -f --tail=100

logs-%: ## Logs de um serviço específico (ex: make logs-inbox)
	docker compose logs -f --tail=200 $*

logs-all: ## Logs agregados com pretty-print JSON + cores por service (LEVEL=error filtra)
	bash framework/scripts/logs.sh

logs-errors: ## Logs agregados, somente warnings+errors
	LEVEL=warning bash framework/scripts/logs.sh

shell-%: ## Bash em um serviço (ex: make shell-inbox)
	docker compose exec $* bash

test: ## Roda testes unitários do framework (pytest dentro do container web)
	docker cp framework/bots/agent_framework agent-framework-web-1:/tmp/agent_framework_src/
	docker compose exec -T web bash -c "cd /tmp/agent_framework_src && pip install --quiet -e .[dev] && pytest tests/ -v"

test-e2e: ## Roda suite Playwright (sobe agentes com CLAUDE_MOCK=1 + pool grande antes)
	@echo "==> ativando CLAUDE_MOCK=1 + POOL_SIZE=10 + IDLE_TIMEOUT_SEC=15 (recreate)..."
	CLAUDE_MOCK=1 POOL_SIZE=10 IDLE_TIMEOUT_SEC=15 \
	  docker compose up -d --force-recreate agent-inbox agent-executor
	@sleep 4
	@echo "==> rodando playwright..."
	cd framework/web/frontend && pnpm exec playwright test

test-e2e-ui: ## Playwright em modo UI interativo
	cd framework/web/frontend && pnpm exec playwright test --ui

reset-agent-%: ## Limpa sessions de um agente (ex: make reset-agent-inbox)
	@# D-51: sessions moram em ${SESSIONS_DIR}/<agent>/ (fora de AGENTS_DIR).
	rm -rf $(or $(SESSIONS_DIR),./instance/sessions)/$*/*
	@# Legacy: limpa tambem $AGENTS_DIR/<agent>/sessions/ se existir (pre-D-51).
	@rm -rf $(or $(AGENTS_DIR),./instance/agents)/$*/sessions 2>/dev/null || true
	@echo "Sessions de $* limpas."

tasks-migrate: ## Backfill idempotente company/tasks/ -> Postgres (D-53)
	docker compose exec -T web python3 /app/migrate_tasks_fs_to_db.py

reset-instance: ## Reseta estado runtime (DB + sessions) preservando config (pede confirmação)
	bash framework/scripts/reset-instance.sh

reset-instance-yes: ## Igual a reset-instance mas sem prompt (perigoso — pra CI/scripts)
	bash framework/scripts/reset-instance.sh --yes

healthcheck: ## Sanity check: containers up + web/db ok + transcriber + tasks
	bash framework/scripts/healthcheck.sh

reconcile: ## Aplica instance/agents/agents.yaml (bots + streams + .env + override + up)
	bash framework/scripts/reconcile.sh

reconcile-dry: ## Mostra o que o reconcile faria, sem aplicar
	bash framework/scripts/reconcile.sh --dry-run

new-agent: ## Adiciona agent novo em agents.yaml (uso: make new-agent NAME=x DISPLAY="X")
	@[ -n "$(NAME)" ] || (echo "use: make new-agent NAME=<slug> DISPLAY=\"<Nome>\""; exit 1)
	@[ -n "$(DISPLAY)" ] || (echo "use: make new-agent NAME=<slug> DISPLAY=\"<Nome>\""; exit 1)
	bash framework/scripts/new-agent.sh $(NAME) "$(DISPLAY)"

migrate: ## Aplica migrations pendentes (normalmente roda sozinho no boot do web)
	docker compose exec -T web python3 /app/migrate.py

migrate-status: ## Lista migrations aplicadas/pendentes
	docker compose exec -T web python3 /app/migrate.py --status

migrate-baseline: ## Marca migrations como aplicadas sem executar (uso: make migrate-baseline V=1)
	@[ -n "$(V)" ] || (echo "use: make migrate-baseline V=<version>"; exit 1)
	docker compose exec -T web python3 /app/migrate.py --baseline $(V)
