-- 028_messaging_is_active.sql
-- Soft-delete for users and streams that belong to deactivated agents.
--
-- Problem: when an agent leaves instance/agents/agents.yaml, the current
-- reconcile does not remove its row in messaging.users / messaging.streams. Leftover
-- artifacts (`tech-lead-bot`, stream `tech-lead`) kept showing up
-- in `_team_block` (claude_runner builds '## Team' by reading all bots) and in
-- PWA listings. The PO then got "tech-lead" in its system prompt as a
-- valid peer and called `ask_agent(target_agent='tech-lead')` — the child
-- conv was created but stalled because there is no container.
--
-- Decision: add `is_active boolean DEFAULT true` to both tables
-- (soft-delete preserves the history of old convs that reference these
-- streams/users). Reconcile now sets `is_active=false` when an
-- agent leaves the yaml. UI/prompt filters use `is_active=true`.

ALTER TABLE messaging.users
    ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT true;

ALTER TABLE messaging.streams
    ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT true;

COMMENT ON COLUMN messaging.users.is_active IS
    'false = agent removed from agents.yaml (soft delete). Filtered out of '
    'system prompts (## Team) and PWA listings. History is kept.';

COMMENT ON COLUMN messaging.streams.is_active IS
    'false = stream of an agent removed from agents.yaml (soft delete). '
    'Old conversations stay accessible but the stream is hidden in UIs.';

CREATE INDEX IF NOT EXISTS users_active_bot_idx
    ON messaging.users (agent_name)
 WHERE kind = 'bot' AND is_active = true AND agent_name IS NOT NULL;

CREATE INDEX IF NOT EXISTS streams_active_idx
    ON messaging.streams (name)
 WHERE is_active = true;
