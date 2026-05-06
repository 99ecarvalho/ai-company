-- 022: backfill corrigido do conversation_id em telemetry.events.
--
-- A migration 020 tentou matchear `c.topic_name = e.topic_slug`, mas topic_slug
-- e '<stream>__<topic>' (TopicKey.slug()) enquanto c.topic_name e so o topic.
-- Aqui dividimos corretamente: parte 1 como stream (via JOIN streams) e parte
-- 2 como topic_name. Condicao de ambiguidade continua: so atribui quando
-- existe match unico.

UPDATE telemetry.events e
   SET conversation_id = c.id
  FROM messaging.conversations c
  JOIN messaging.streams s ON s.id = c.stream_id
 WHERE e.conversation_id IS NULL
   AND e.topic_slug IS NOT NULL
   AND e.topic_slug LIKE '%\_\_%' ESCAPE '\'
   AND s.name       = split_part(e.topic_slug, '__', 1)
   AND c.topic_name = split_part(e.topic_slug, '__', 2);
