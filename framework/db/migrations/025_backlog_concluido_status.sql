-- 025_backlog_concluido_status.sql
-- Adiciona 'concluido' ao vocabulario de status de tasks.backlog.
--
-- Semantica: item ja foi promovido (gerou tasks.tasks) E a task chegou em
-- terminal `done` via complete_phase. Antes desta migration o backlog
-- ficava preso em 'promovido' eternamente — o humano nao distinguia
-- visualmente o que ja foi entregue do que ainda esta em execucao na
-- coluna 'Promoted' do kanban.
--
-- Transicao 'promovido' -> 'concluido' eh disparada em
-- workflow.complete_phase (D-102) quando `next_='done'` e a task tem um
-- backlog associado (promoted_task_slug = task.slug). Backfill abaixo
-- corrige items historicos cujas tasks ja chegaram em done antes deste
-- hook existir — sem isso o kanban continuaria mostrando entregas
-- antigas como 'promovido' indefinidamente.
--
-- Conjunto completo apos esta migration: aberto | rascunho | em_execucao
-- | promovido | concluido | descartado.

ALTER TABLE tasks.backlog
    DROP CONSTRAINT tasks_backlog_status_chk;

ALTER TABLE tasks.backlog
    ADD CONSTRAINT tasks_backlog_status_chk
    CHECK (status IN ('aberto', 'rascunho', 'em_execucao', 'promovido', 'concluido', 'descartado'));

COMMENT ON COLUMN tasks.backlog.status IS
    'aberto = triado e aguardando priorizacao; '
    'rascunho = brain-dump sem classificacao, aguardando especificacao humana; '
    'em_execucao = convertido em task ativa (legacy/manual); '
    'promovido = virou task via backlog_promote, task em andamento; '
    'concluido = task atingiu terminal done via complete_phase; '
    'descartado = arquivado sem promover.';

-- Backfill historico: items 'promovido' cuja task ja esta em 'done'.
UPDATE tasks.backlog b
   SET status = 'concluido', updated_at = now()
  FROM tasks.tasks t
 WHERE b.promoted_task_slug = t.slug
   AND t.status = 'done'
   AND b.status = 'promovido';
