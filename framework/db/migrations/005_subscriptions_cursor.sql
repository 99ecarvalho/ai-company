-- 005_subscriptions_cursor.sql
-- Cursor persisted per (user_id, stream_id) for message catch-up
-- when an agent restarts. pg_notify is fire-and-forget: if the bot went down
-- between the INSERT and _on_notify running, the msg became a ghost. With a
-- cursor, start() runs SELECT id FROM messaging.messages WHERE id > last_read
-- and replays before enabling LISTEN. Semantics:
--   NULL = never read anything (new bot) → catch-up may skip the historical
--          backlog OR replay everything. We chose: NULL == 0 in WHERE id > $,
--          but on first start the agent sets it to the current max(id) so it does
--          not replay the whole history on install.
--   N    = next catch-up fetches WHERE id > N.
-- Update: done by the broker endpoint POST /api/subscriptions/cursor, called
-- by the agent after enqueuing the msg in the internal pipeline.

ALTER TABLE messaging.subscriptions
    ADD COLUMN IF NOT EXISTS last_read_message_id BIGINT;
