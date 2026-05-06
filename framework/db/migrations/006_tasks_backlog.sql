-- 006_tasks_backlog.sql
-- Move metadata estruturada de tasks e items de backlog pra Postgres (D-53).
-- Antes vivia em company/tasks/<slug>/metadata.yaml e company/backlog/*.md,
-- com 3 consumidores lendo/escrevendo: WorkflowManager (agentes via MCP),
-- web /api/tasks (PWA lista), reactor (handoffs). Mistura duas coisas:
--   - metadata ESTRUTURADA (slug, status, phases, current_agent, ...)
--   - artifacts MARKDOWN (00-triagem.md, 01-analise.md, ...)
-- Artifacts ficam em filesystem (agentes escrevem via Claude Code Write
-- tool, PWA le e renderiza). Metadata vira banco — query O(1), lock-free,
-- sem race de YAML tmp+rename, e libera `company/` pra nao versionar estado
-- temporal. Backlog inteiro (conteudo + prioridade) cabe no banco — items
-- sao pequenos e 100% estruturados.
--
-- Schema externaliza-se pelo pacote MCP: agentes chamam backlog_* / task_*
-- em vez de mkdir/mv/yaml no filesystem. Integracao futura com gitlab/github
-- issues plugaria em cima de um 'origin_kind + origin_url' no backlog.

CREATE SCHEMA IF NOT EXISTS tasks;

-- Tasks: 1 por slug. Campos historicos preservados em metadata_extra JSONB
-- pra nao perder info quando o schema yaml nao se encaixa perfeitamente
-- (assigned_topic, __started_at, baseline vindo de analise, etc).
CREATE TABLE tasks.tasks (
    id                BIGSERIAL PRIMARY KEY,
    slug              TEXT UNIQUE NOT NULL,
    title             TEXT NOT NULL,
    workflow          TEXT,                              -- nome do workflow (da instance, opcional)
    status            TEXT NOT NULL DEFAULT 'in_progress',  -- in_progress|done|halt|human_review|blocked
    current_step      TEXT,
    current_agent     TEXT,
    complexity        TEXT,                              -- pequeno|medio|grande|livre
    impact            TEXT,                              -- alto|medio|baixo
    difficulty        TEXT,
    origin            TEXT,                              -- descricao livre (freeform)
    origin_stream     TEXT,
    origin_topic      TEXT,
    blocked_reason    TEXT,
    metadata_extra    JSONB NOT NULL DEFAULT '{}'::jsonb,
    archived_at       TIMESTAMPTZ,                       -- null = ativa
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX tasks_tasks_status_updated ON tasks.tasks (status, updated_at DESC);
CREATE INDEX tasks_tasks_archived       ON tasks.tasks (archived_at) WHERE archived_at IS NOT NULL;

-- Phases: historico ordenado. idx define ordem cronologica (complete_phase
-- sempre append; fase em progresso tem completed_at NULL).
CREATE TABLE tasks.phases (
    id            BIGSERIAL PRIMARY KEY,
    task_id       BIGINT NOT NULL REFERENCES tasks.tasks(id) ON DELETE CASCADE,
    idx           INTEGER NOT NULL,                      -- 0, 1, 2, ... cronologico
    step          TEXT NOT NULL,
    agent         TEXT,
    started_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ,
    artifact      TEXT,                                  -- nome do .md em company/tasks/<slug>/
    summary       TEXT,
    UNIQUE (task_id, idx)
);
CREATE INDEX tasks_phases_task ON tasks.phases (task_id);

-- Worktrees: registradas via register_worktree MCP. 1 task -> N worktrees
-- (multi-repo). Composite unique evita dup accidental por agente distraido.
CREATE TABLE tasks.worktrees (
    id           BIGSERIAL PRIMARY KEY,
    task_id      BIGINT NOT NULL REFERENCES tasks.tasks(id) ON DELETE CASCADE,
    repo         TEXT NOT NULL,
    branch       TEXT NOT NULL,
    path         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (task_id, repo, branch)
);

-- Backlog: items soltos aguardando priorizacao. Conteudo markdown cabe no
-- banco (sao notas, nao artifacts complexos). Quando um item vira task, a
-- MCP tool backlog_promote cria a task e preenche promoted_task_slug —
-- item fica como historico linkado (`status='promovido'`).
CREATE TABLE tasks.backlog (
    id                   BIGSERIAL PRIMARY KEY,
    slug                 TEXT UNIQUE NOT NULL,           -- kebab, unico dentro da instance
    title                TEXT NOT NULL,
    content              TEXT,                           -- markdown livre
    priority             INTEGER NOT NULL DEFAULT 0,     -- convencao: -2..+2 (baixo..critico)
    impact               TEXT,                           -- alto|medio|baixo
    effort               TEXT,                           -- alto|medio|baixo
    status               TEXT NOT NULL DEFAULT 'aberto', -- aberto|em_execucao|promovido|descartado
    promoted_task_slug   TEXT REFERENCES tasks.tasks(slug) ON DELETE SET NULL,
    created_by           TEXT,                           -- agent name ou 'user'
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX tasks_backlog_status_prio ON tasks.backlog (status, priority DESC, updated_at DESC);

-- Trigger: updated_at auto em UPDATE.
CREATE OR REPLACE FUNCTION tasks._touch_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER tasks_tasks_touch
    BEFORE UPDATE ON tasks.tasks
    FOR EACH ROW EXECUTE FUNCTION tasks._touch_updated_at();

CREATE TRIGGER tasks_backlog_touch
    BEFORE UPDATE ON tasks.backlog
    FOR EACH ROW EXECUTE FUNCTION tasks._touch_updated_at();
