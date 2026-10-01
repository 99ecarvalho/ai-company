-- 022: corrected backfill of conversation_id in telemetry.events.
--
-- Migration 020 tried to match `c.topic_name = e.topic_slug`, but topic_slug
-- is '<stream>__<topic>' (TopicKey.slug()) while c.topic_name is just the topic.
-- Here we split correctly: part 1 as stream (via JOIN streams) and part
-- 2 as topic_name. The ambiguity condition still holds: only assigns when
-- there is a unique match.

UPDATE telemetry.events e
   SET conversation_id = c.id
  FROM messaging.conversations c
  JOIN messaging.streams s ON s.id = c.stream_id
 WHERE e.conversation_id IS NULL
   AND e.topic_slug IS NOT NULL
   AND e.topic_slug LIKE '%\_\_%' ESCAPE '\'
   AND s.name       = split_part(e.topic_slug, '__', 1)
   AND c.topic_name = split_part(e.topic_slug, '__', 2);
