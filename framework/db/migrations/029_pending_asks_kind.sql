-- 029_pending_asks_kind.sql
-- Persist ask_agent in pending_asks so it survives an agent restart.
--
-- Problem: ask_agent (agent A asks agent B) was in-memory state
-- in agent A's process. Container restart = asyncio Future
-- lost, agent A never wakes up when B answers, conv stalls. Real case
-- 2026-04-30 in conv database-engineer/2026-04-30 13:09: a rebuild killed DBE
-- mid-ask_agent. It recovered by luck (claude --resume re-emitted the ask_agent
-- + RG unread catch-up), but at a cost: duplicated question in conv 7164.
--
-- Decision: persist ask_agent in messaging.pending_asks with kind='ask_agent'.
-- The current auto-resolve (UPDATE when a non-asker posts) already covers it.
-- Restart recovery: when ask_agent is re-emitted (claude --resume), the broker sees
-- the resolved pending_ask and returns the answer directly without reposting or waiting.
--
-- IMPORTANT: ask_agent must still not trigger VAPID push nor show up in
-- "Mine" (the human's needs-you badge). A `kind='ask_human'` filter is applied at the
-- UI/push points (in code, not schema).

ALTER TABLE messaging.pending_asks
    ADD COLUMN kind TEXT NOT NULL DEFAULT 'ask_human'
        CHECK (kind IN ('ask_human', 'ask_agent'));

ALTER TABLE messaging.pending_asks
    ADD COLUMN target_agent TEXT;

COMMENT ON COLUMN messaging.pending_asks.kind IS
    'ask_human = a human must answer (push + Mine badge). '
    'ask_agent = the target agent must answer (silent for humans). '
    'Defaults to ask_human for compatibility with pre-D-111 rows.';

COMMENT ON COLUMN messaging.pending_asks.target_agent IS
    'When kind=ask_agent, name of the agent that must answer. '
    'Informational: auto-resolve fires on any sender != asker.';

-- Index for lookup by (asker, conv, kind) on re-emission after restart.
CREATE INDEX IF NOT EXISTS pending_asks_asker_kind_idx
    ON messaging.pending_asks (asker_id, conversation_id, kind)
 WHERE resolved_at IS NULL;
