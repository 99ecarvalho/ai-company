-- 021: corrected backfill of task_slug in telemetry.events.
--
-- Migration 020 backfilled with `topic_slug LIKE 'task-%'`, but I forgot
-- that the runner builds `topic_slug = '<stream>__<topic>'` (see TopicKey.slug
-- in internal_client.py). The original LIKE matched nothing in production.
-- Here we extract the part after the first '__' and check whether it starts
-- with 'task-'. Idempotent: only updates rows with task_slug IS NULL.

UPDATE telemetry.events
   SET task_slug = substring(split_part(topic_slug, '__', 2) FROM 6)
 WHERE task_slug IS NULL
   AND topic_slug LIKE '%\_\_task-%' ESCAPE '\';
