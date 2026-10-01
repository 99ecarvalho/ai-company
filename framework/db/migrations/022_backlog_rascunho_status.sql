-- 022_backlog_rascunho_status.sql
-- Adds 'rascunho' to the tasks.backlog status vocabulary.
--
-- Semantics: item created in raw capture mode (brain-dump). The human
-- dropped an idea without classifying it; product-owner records it verbatim without
-- interviewing. Specification stays pending until the human (or the daily
-- curation) returns to the item.
--
-- Not included in the default backlog_list (status='aberto') — only shows up
-- when the caller explicitly asks for status='rascunho' or status='all'.
-- The product-owner's daily briefing should query rascunho WHERE age>7d.
--
-- Full set after this migration: aberto | rascunho |
-- em_execucao | promovido | descartado. The CHECK constraint formalizes the
-- enum — prevents silent typos (the column was free TEXT).

ALTER TABLE tasks.backlog
    ADD CONSTRAINT tasks_backlog_status_chk
    CHECK (status IN ('aberto', 'rascunho', 'em_execucao', 'promovido', 'descartado'));

COMMENT ON COLUMN tasks.backlog.status IS
    'aberto = triaged, waiting for prioritization; '
    'rascunho = unclassified brain dump, waiting for a human to specify it; '
    'em_execucao = turned into an active task (legacy/manual); '
    'promovido = became a task through backlog_promote (linked via promoted_task_slug); '
    'descartado = archived without being promoted.';
