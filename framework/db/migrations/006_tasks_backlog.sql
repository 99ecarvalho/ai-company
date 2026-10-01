-- 006_tasks_backlog.sql
-- Moves structured metadata of tasks and backlog items to Postgres (D-53).
-- It used to live in company/tasks/<slug>/metadata.yaml and company/backlog/*.md,
-- with 3 consumers reading/writing: WorkflowManager (agents via MCP),
-- web /api/tasks (PWA list), reactor (handoffs). It mixed two things:
--   - STRUCTURED metadata (slug, status, phases, current_agent, ...)
--   - MARKDOWN artifacts (00-triagem.md, 01-analise.md, ...)
-- Artifacts stay on the filesystem (agents write via the Claude Code Write
-- tool, PWA reads and renders). Metadata moves to the DB — O(1) query, lock-free,
-- no YAML tmp+rename race, and frees `company/` from versioning temporal
-- state. The whole backlog (content + priority) fits in the DB — items
-- are small and 100% structured.
--
-- The schema is exposed through the MCP package: agents call backlog_* / task_*
-- instead of mkdir/mv/yaml on the filesystem. Future gitlab/github issues
-- integration would plug in on top of 'origin_kind + origin_url' in the backlog.

CREATE SCHEMA IF NOT EXISTS tasks;

-- Tasks: 1 per slug. Historical fields preserved in metadata_extra JSONB
-- so no info is lost when the yaml schema does not fit perfectly
-- (assigned_topic, __started_at, baseline coming from analysis, etc).
CREATE TABLE tasks.tasks (
    id                BIGSERIAL PRIMARY KEY,
    slug              TEXT UNIQUE NOT NULL,
    title             TEXT NOT NULL,
    workflow          TEXT,                              -- workflow name (from the instance, optional)
    status            TEXT NOT NULL DEFAULT 'in_progress',  -- in_progress|done|halt|human_review|blocked
    current_step      TEXT,
    current_agent     TEXT,
    complexity        TEXT,                              -- pequeno|medio|grande|livre
    impact            TEXT,                              -- alto|medio|baixo
    difficulty        TEXT,
    origin            TEXT,                              -- free-form description
    origin_stream     TEXT,
    origin_topic      TEXT,
    blocked_reason    TEXT,
    metadata_extra    JSONB NOT NULL DEFAULT '{}'::jsonb,
    archived_at       TIMESTAMPTZ,                       -- null = active
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX tasks_tasks_status_updated ON tasks.tasks (status, updated_at DESC);
CREATE INDEX tasks_tasks_archived       ON tasks.tasks (archived_at) WHERE archived_at IS NOT NULL;

-- Phases: ordered history. idx defines chronological order (complete_phase
-- always appends; the phase in progress has completed_at NULL).
CREATE TABLE tasks.phases (
    id            BIGSERIAL PRIMARY KEY,
    task_id       BIGINT NOT NULL REFERENCES tasks.tasks(id) ON DELETE CASCADE,
    idx           INTEGER NOT NULL,                      -- 0, 1, 2, ... chronological
    step          TEXT NOT NULL,
    agent         TEXT,
    started_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ,
    artifact      TEXT,                                  -- .md file name in company/tasks/<slug>/
    summary       TEXT,
    UNIQUE (task_id, idx)
);
CREATE INDEX tasks_phases_task ON tasks.phases (task_id);

-- Worktrees: registered via the register_worktree MCP. 1 task -> N worktrees
-- (multi-repo). Composite unique prevents accidental dups by a distracted agent.
CREATE TABLE tasks.worktrees (
    id           BIGSERIAL PRIMARY KEY,
    task_id      BIGINT NOT NULL REFERENCES tasks.tasks(id) ON DELETE CASCADE,
    repo         TEXT NOT NULL,
    branch       TEXT NOT NULL,
    path         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (task_id, repo, branch)
);

-- Backlog: loose items awaiting prioritization. Markdown content fits in the
-- DB (they are notes, not complex artifacts). When an item becomes a task, the
-- backlog_promote MCP tool creates the task and fills promoted_task_slug —
-- the item stays as linked history (`status='promovido'`).
CREATE TABLE tasks.backlog (
    id                   BIGSERIAL PRIMARY KEY,
    slug                 TEXT UNIQUE NOT NULL,           -- kebab, unique within the instance
    title                TEXT NOT NULL,
    content              TEXT,                           -- free markdown
    priority             INTEGER NOT NULL DEFAULT 0,     -- convention: -2..+2 (low..critical)
    impact               TEXT,                           -- alto|medio|baixo
    effort               TEXT,                           -- alto|medio|baixo
    status               TEXT NOT NULL DEFAULT 'aberto', -- aberto|em_execucao|promovido|descartado
    promoted_task_slug   TEXT REFERENCES tasks.tasks(slug) ON DELETE SET NULL,
    created_by           TEXT,                           -- agent name or 'user'
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX tasks_backlog_status_prio ON tasks.backlog (status, priority DESC, updated_at DESC);

-- Trigger: auto updated_at on UPDATE.
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
