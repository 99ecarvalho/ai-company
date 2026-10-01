-- 011: tombstones for deleted topics to prevent resurrection after DELETE.
--
-- Problem (D-72): when the human deletes a conversation via the PWA, the backend
-- DELETEs from `messaging.conversations` and fires a cancel via pg_notify
-- to release active runners. The dispatcher answers the cancel by posting a
-- confirmation msg ("cancelled by user" or "nothing to cancel"). That
-- msg goes through `_get_or_create_conversation`, which AUTO-CREATES the conv if it
-- does not exist — result: the conv comes back with the dispatcher's message, undoing
-- the human's delete.
--
-- Fix: record a tombstone `(stream_id, topic_name, deleted_at)` before the
-- DELETE. `_get_or_create_conversation` checks the tombstone: if deleted less
-- than `TOMBSTONE_TTL_SEC` ago (default 5min, env), raise 410 Gone — the dispatcher
-- logs a warn, drops the msg, and the pool slot is released without resurrecting
-- the conv.
--
-- Short TTL (5min) because `__ask-from-<agent>-<uid>` topics have a random uid and
-- do not collide; an expired tombstone is harmless. Periodic cleanup via a
-- scheduler job (future) or a simple DELETE WHERE deleted_at < now() - interval.
--
-- Additive: new table, zero impact on existing schema. Trivially reversible.

CREATE TABLE messaging.deleted_topics (
    stream_id  INTEGER NOT NULL REFERENCES messaging.streams(id) ON DELETE CASCADE,
    topic_name TEXT NOT NULL,
    deleted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (stream_id, topic_name)
);

-- Index for fast lookup by time (cleanup + TTL check).
CREATE INDEX idx_deleted_topics_deleted_at
    ON messaging.deleted_topics (deleted_at);
