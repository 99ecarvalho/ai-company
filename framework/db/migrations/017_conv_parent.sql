-- 017: parent_conv_id explicito em messaging.conversations (D-87).
--
-- Motivo: hierarquia pai-filha era inferida por heuristica em
-- _compute_hierarchy (broker.py) — parsing de topic `__ask-from-CHAIN-uid`
-- + correlacao com tool_use ask_agent em telemetry.live_events. Frágil
-- pra multi-level (chain de 2+ niveis dava parent_agent errado) e
-- requer eventos sinteticos (D-86) pra subagents inline. Solucao
-- "de uma vez por todas": persistir parent_conv_id direto na tabela
-- no momento de criacao da conv. Agente/reactor passam o asker_conv_id
-- explicito; broker armazena. Hierarquia vira lookup O(1) trivial.
--
-- Convs existentes (pre-migration) ficam com NULL — heuristica antiga
-- continua como fallback. Backfill opcional via script separado.

ALTER TABLE messaging.conversations
  ADD COLUMN parent_conv_id INTEGER NULL
  REFERENCES messaging.conversations(id) ON DELETE SET NULL;

CREATE INDEX idx_conversations_parent
  ON messaging.conversations(parent_conv_id)
  WHERE parent_conv_id IS NOT NULL;
