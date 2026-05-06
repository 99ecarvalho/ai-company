-- 019: conversation_id + task_slug em telemetry.events.
--
-- Problema: hoje telemetry.events nao tem FK pra messaging.conversations —
-- a atribuicao vem so pelo `topic_slug` (TEXT). Quando a conv e deletada,
-- o row sobrevive (bom pra analytics/billing) mas fica orfao: perde a
-- ligacao relacional e, se o topic_slug for reusado, mistura dados. Alem
-- disso, cost/effort por task e calculado hoje via LIKE/match de
-- `topic_slug = 'task-<slug>'` — fragil.
--
-- Fix:
--   * `conversation_id` com ON DELETE SET NULL — row sobrevive ao delete,
--     perde so o ponteiro vivo. Diferente de `telemetry.live_events`
--     (CASCADE) porque aqui cost/tokens valem mais do que a ligacao.
--   * `task_slug` TEXT (nao FK) — snapshot do slug. Sobrevive a rename/
--     delete de task. Matching futuro com tasks.tasks.slug e best-effort.
--
-- Backfill one-time:
--   (a) task_slug: extrai de topic_slug quando casa 'task-<X>'.
--   (b) conversation_id: resolve via messaging.conversations.topic_name
--       quando unico. Se ambiguo (topic_slug reusado em streams diferentes)
--       fica NULL — o valor e pros dados novos, nao pros historicos.

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

-- Backfill conversation_id (so quando ha match unico de topic_name)
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
