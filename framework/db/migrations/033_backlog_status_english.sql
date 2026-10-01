-- 033: backlog status values in English.
--
-- The vocabulary was Portuguese (aberto, rascunho, em_execucao, promovido,
-- concluido, descartado). Rename it in place, same meanings:
--   aberto -> open, rascunho -> draft, em_execucao -> in_progress,
--   promovido -> promoted, concluido -> done, descartado -> discarded.
-- The web API and the backlog_* MCP tools still accept the old values as
-- input and map them to the new ones.
ALTER TABLE tasks.backlog DROP CONSTRAINT IF EXISTS tasks_backlog_status_chk;

UPDATE tasks.backlog SET status = CASE status
    WHEN 'aberto' THEN 'open'
    WHEN 'rascunho' THEN 'draft'
    WHEN 'em_execucao' THEN 'in_progress'
    WHEN 'promovido' THEN 'promoted'
    WHEN 'concluido' THEN 'done'
    WHEN 'descartado' THEN 'discarded'
    ELSE status
END;

ALTER TABLE tasks.backlog ALTER COLUMN status SET DEFAULT 'open';
ALTER TABLE tasks.backlog ADD CONSTRAINT tasks_backlog_status_chk
    CHECK (status IN ('open', 'draft', 'in_progress', 'promoted', 'done', 'discarded'));

COMMENT ON COLUMN tasks.backlog.status IS
    'open = triaged, waiting for prioritization; '
    'draft = unclassified brain dump, waiting for a human to specify it; '
    'in_progress = turned into an active task (legacy/manual); '
    'promoted = became a task through backlog_promote, task in progress; '
    'done = the task reached the done terminal through complete_phase; '
    'discarded = archived without being promoted.';
