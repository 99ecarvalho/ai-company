-- 025_backlog_concluido_status.sql
-- Adds 'concluido' to the tasks.backlog status vocabulary.
--
-- Semantics: the item was already promoted (created tasks.tasks) AND the task reached
-- the `done` terminal via complete_phase. Before this migration the backlog
-- was stuck in 'promovido' forever — the human could not visually tell
-- what had been delivered from what was still in progress in the
-- kanban's 'Promoted' column.
--
-- The 'promovido' -> 'concluido' transition is fired in
-- workflow.complete_phase (D-102) when `next_='done'` and the task has an
-- associated backlog item (promoted_task_slug = task.slug). The backfill below
-- fixes historical items whose tasks reached done before this
-- hook existed — without it the kanban would keep showing old
-- deliveries as 'promovido' indefinitely.
--
-- Full set after this migration: aberto | rascunho | em_execucao
-- | promovido | concluido | descartado.

ALTER TABLE tasks.backlog
    DROP CONSTRAINT tasks_backlog_status_chk;

ALTER TABLE tasks.backlog
    ADD CONSTRAINT tasks_backlog_status_chk
    CHECK (status IN ('aberto', 'rascunho', 'em_execucao', 'promovido', 'concluido', 'descartado'));

COMMENT ON COLUMN tasks.backlog.status IS
    'aberto = triaged, waiting for prioritization; '
    'rascunho = unclassified brain dump, waiting for a human to specify it; '
    'em_execucao = turned into an active task (legacy/manual); '
    'promovido = became a task through backlog_promote, task in progress; '
    'concluido = the task reached the done terminal through complete_phase; '
    'descartado = archived without being promoted.';

-- Historical backfill: 'promovido' items whose task is already 'done'.
UPDATE tasks.backlog b
   SET status = 'concluido', updated_at = now()
  FROM tasks.tasks t
 WHERE b.promoted_task_slug = t.slug
   AND t.status = 'done'
   AND b.status = 'promovido';
