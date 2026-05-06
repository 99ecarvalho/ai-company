-- 008: archived_at em messaging.conversations pra soft-delete manual de threads
-- pelo humano no PWA.
--
-- Parte do plano D-57 (unificacao tasks <-> threads + Active/Closed manual):
-- a PWA passa a ter *uma* lista de threads (mata abas Mine/Background/Tasks),
-- com dois filtros Active (archived_at IS NULL) e Closed (archived_at IS NOT
-- NULL). Decisao de fechar/reabrir e sempre do humano, nunca automatica —
-- nem quando a task atrelada bate terminal (done/halt/human_review), a thread
-- fecha sozinha.
--
-- Aditivo: NULL default preserva comportamento atual (todas as convs existentes
-- viram Active). Reversivel trivialmente (DROP COLUMN).

ALTER TABLE messaging.conversations
  ADD COLUMN archived_at TIMESTAMPTZ;

CREATE INDEX conversations_archived_idx
  ON messaging.conversations (archived_at)
  WHERE archived_at IS NOT NULL;
