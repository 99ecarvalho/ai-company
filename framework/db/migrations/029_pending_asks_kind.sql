-- 029_pending_asks_kind.sql
-- Persistencia de ask_agent em pending_asks pra sobreviver a restart de agente.
--
-- Problema: ask_agent (agente A pergunta a agente B) era estado em-memoria
-- no processo do agente A. Restart do container = futuro Future asyncio
-- perdido, agente A nao acorda quando B responde, conv parada. Caso real
-- 2026-04-30 na conv database-engineer/2026-04-30 13:09: rebuild matou DBE
-- mid-ask_agent. Recuperou por sorte (claude --resume re-emitiu o ask_agent
-- + RG catch-up de unread), mas custo: question duplicada em conv 7164.
--
-- Decisao: persistir ask_agent em messaging.pending_asks com kind='ask_agent'.
-- Auto-resolve atual (UPDATE quando alguem nao-asker posta) ja cobre.
-- Restart-recovery: na re-emissao do ask_agent (claude --resume), broker ve
-- pending_ask resolvido, retorna a resposta direto sem repostar nem aguardar.
--
-- IMPORTANTE: ask_agent ainda nao deve disparar push VAPID nem aparecer em
-- "Mine" (badge needs-you do humano). Filtro `kind='ask_human'` aplicado nos
-- pontos de UI/push (em codigo, nao schema).

ALTER TABLE messaging.pending_asks
    ADD COLUMN kind TEXT NOT NULL DEFAULT 'ask_human'
        CHECK (kind IN ('ask_human', 'ask_agent'));

ALTER TABLE messaging.pending_asks
    ADD COLUMN target_agent TEXT;

COMMENT ON COLUMN messaging.pending_asks.kind IS
    'ask_human = humano deve responder (push + Mine badge). '
    'ask_agent = agente target deve responder (silencioso pra humano). '
    'Default ask_human pra compat com rows pre-D-111.';

COMMENT ON COLUMN messaging.pending_asks.target_agent IS
    'Quando kind=ask_agent, nome do agente que deve responder. '
    'Informativo — auto-resolve dispara em qualquer sender != asker.';

-- Indice pra busca por (asker, conv, kind) na re-emissao apos restart.
CREATE INDEX IF NOT EXISTS pending_asks_asker_kind_idx
    ON messaging.pending_asks (asker_id, conversation_id, kind)
 WHERE resolved_at IS NULL;
