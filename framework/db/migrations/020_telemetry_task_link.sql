-- 020: conversation_id + task_slug on telemetry.events.
--
-- Problem: telemetry.events currently has no FK to messaging.conversations —
-- attribution comes only from `topic_slug` (TEXT). When the conv is deleted,
-- the row survives (good for analytics/billing) but becomes orphaned: it loses the
-- relational link and, if the topic_slug is reused, data gets mixed. Also,
-- cost/effort per task is currently computed via LIKE/match on
-- `topic_slug = 'task-<slug>'` — fragile.
--
-- Fix:
--   * `conversation_id` with ON DELETE SET NULL — the row survives the delete,
--     losing only the live pointer. Unlike `telemetry.live_events`
--     (CASCADE) because here cost/tokens are worth more than the link.
--   * `task_slug` TEXT (not FK) — snapshot of the slug. Survives task rename/
--     delete. Future matching against tasks.tasks.slug is best-effort.
--
-- One-time backfill:
--   (a) task_slug: extracted from topic_slug when it matches 'task-<X>'.
--   (b) conversation_id: resolved via messaging.conversations.topic_name
--       when unique. If ambiguous (topic_slug reused across streams)
--       it stays NULL — the value is for new data, not historical data.

ALTER TABLE telemetry.events
    ADD COLUMN IF NOT EXISTS conversation_id INTEGER REFERENCES messaging.conversations(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS task_slug       TEXT;

CREATE INDEX IF NOT EXISTS telemetry_events_conv ON telemetry.events (conversation_id);
CREATE INDEX IF NOT EXISTS telemetry_events_task ON telemetry.events (task_slug) WHERE task_slug IS NOT NULL;

-- Backfill task_slug
UPDATE telemetry.events
   SET task_slug = substring(topic_slug FROM 6)
 WHERE task_slug IS NULL
   AND topic_slug LIKE 'task-%';

-- Backfill conversation_id (only when there is a unique topic_name match)
UPDATE telemetry.events e
   SET conversation_id = c.id
  FROM messaging.conversations c
 WHERE e.conversation_id IS NULL
   AND e.topic_slug IS NOT NULL
   AND c.topic_name = e.topic_slug
   AND NOT EXISTS (
       SELECT 1 FROM messaging.conversations c2
        WHERE c2.topic_name = e.topic_slug AND c2.id <> c.id
   );
