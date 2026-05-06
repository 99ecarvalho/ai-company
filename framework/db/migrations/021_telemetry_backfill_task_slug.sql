-- 021: backfill corrigido do task_slug em telemetry.events.
--
-- A migration 020 fez backfill com `topic_slug LIKE 'task-%'`, mas esqueci
-- que o runner compoe `topic_slug = '<stream>__<topic>'` (ver TopicKey.slug
-- em internal_client.py). O LIKE original nao casou nada em producao.
-- Aqui extraimos a parte apos o primeiro '__' e verificamos se comeca
-- com 'task-'. Idempotente: so atualiza rows com task_slug IS NULL.

UPDATE telemetry.events
   SET task_slug = substring(split_part(topic_slug, '__', 2) FROM 6)
 WHERE task_slug IS NULL
   AND topic_slug LIKE '%\_\_task-%' ESCAPE '\';
