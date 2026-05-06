-- 022_backlog_rascunho_status.sql
-- Adiciona 'rascunho' ao vocabulario de status de tasks.backlog.
--
-- Semantica: item criado em modo captura crua (brain-dump). O humano
-- jogou uma ideia sem classificar; product-owner registra literal sem
-- entrevistar. Especificacao fica pendente ate o humano (ou a curadoria
-- diaria) voltar ao item.
--
-- Nao entra em backlog_list default (status='aberto') — so aparece
-- quando o caller pede explicitamente status='rascunho' ou status='all'.
-- Daily briefing do product-owner deve querar rascunho WHERE age>7d.
--
-- Conjunto completo apos esta migration: aberto | rascunho |
-- em_execucao | promovido | descartado. CHECK constraint formaliza o
-- enum — previne typo silencioso (coluna era TEXT livre).

ALTER TABLE tasks.backlog
    ADD CONSTRAINT tasks_backlog_status_chk
    CHECK (status IN ('aberto', 'rascunho', 'em_execucao', 'promovido', 'descartado'));

COMMENT ON COLUMN tasks.backlog.status IS
    'aberto = triado e aguardando priorizacao; '
    'rascunho = brain-dump sem classificacao, aguardando especificacao humana; '
    'em_execucao = convertido em task ativa (legacy/manual); '
    'promovido = virou task via backlog_promote (linkado via promoted_task_slug); '
    'descartado = arquivado sem promover.';
