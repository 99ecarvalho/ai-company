-- D-84 fixture: plants 6 convs covering each state of the unified model.
-- Idempotent — cleans up the fixture's existing convs before inserting.
--
-- States covered:
--   1. idle           — run_end success, no new triggers
--   2. running        — run_start without run_end, recent ts
--   3. stuck          — run_start without run_end, ts > 600s
--   4. awaiting_human — pending_ask unresolved
--   5. errored        — run_end with subtype error_*
--   6. parent + stuck child — cascade to children_stats.stuck

BEGIN;

-- Clean up previous fixtures (topics with the `_test-state-` prefix)
DELETE FROM messaging.conversations
 WHERE topic_name LIKE '_test-state-%';

-- Stream_id for product-owner = 1, executor-legado = 4 (verified).
-- User: alice = 1; bots: product-owner-bot = 3, executor-legado-bot = 6.

-- ============================================================
-- CONV 1: IDLE (completed a turn successfully, idle since then)
-- ============================================================
WITH c AS (
  INSERT INTO messaging.conversations (stream_id, topic_name, last_message_at)
  VALUES (1, '_test-state-1-idle', now() - interval '5 minutes')
  RETURNING id
)
INSERT INTO messaging.messages (conversation_id, sender_id, content, sent_at)
SELECT c.id, 1, 'you can start the triage', now() - interval '10 minutes' FROM c
UNION ALL
SELECT c.id, 3, 'done, finished', now() - interval '5 minutes' FROM c;

WITH c AS (SELECT id FROM messaging.conversations WHERE topic_name = '_test-state-1-idle')
INSERT INTO telemetry.live_events (conversation_id, agent, kind, summary, data, ts)
SELECT c.id, 'product-owner', 'run_start', 'turn started', '{}'::jsonb, now() - interval '8 minutes' FROM c
UNION ALL
SELECT c.id, 'product-owner', 'run_end', 'turn complete', '{"subtype":"success"}'::jsonb, now() - interval '5 minutes' FROM c;

-- ============================================================
-- CONV 2: RUNNING (turn in flight, recent ts)
-- ============================================================
WITH c AS (
  INSERT INTO messaging.conversations (stream_id, topic_name, last_message_at)
  VALUES (1, '_test-state-2-running', now() - interval '15 seconds')
  RETURNING id
)
INSERT INTO messaging.messages (conversation_id, sender_id, content, sent_at)
SELECT c.id, 1, 'analyze the backlog and tell me the priorities', now() - interval '20 seconds' FROM c;

WITH c AS (SELECT id FROM messaging.conversations WHERE topic_name = '_test-state-2-running')
INSERT INTO telemetry.live_events (conversation_id, agent, kind, summary, data, ts)
SELECT c.id, 'product-owner', 'run_start', 'turn started', '{}'::jsonb, now() - interval '15 seconds' FROM c;

-- ============================================================
-- CONV 3: STUCK (run_start without end, ts > 600s)
-- ============================================================
WITH c AS (
  INSERT INTO messaging.conversations (stream_id, topic_name, last_message_at)
  VALUES (1, '_test-state-3-stuck', now() - interval '15 minutes')
  RETURNING id
)
INSERT INTO messaging.messages (conversation_id, sender_id, content, sent_at)
SELECT c.id, 1, 'run this really big script for me', now() - interval '20 minutes' FROM c;

WITH c AS (SELECT id FROM messaging.conversations WHERE topic_name = '_test-state-3-stuck')
INSERT INTO telemetry.live_events (conversation_id, agent, kind, summary, data, ts)
SELECT c.id, 'product-owner', 'run_start', 'turn started', '{}'::jsonb, now() - interval '15 minutes' FROM c;

-- ============================================================
-- CONV 4: AWAITING_HUMAN (pending_ask unresolved)
-- ============================================================
WITH c AS (
  INSERT INTO messaging.conversations (stream_id, topic_name, last_message_at)
  VALUES (1, '_test-state-4-awaiting-human', now() - interval '2 minutes')
  RETURNING id
)
INSERT INTO messaging.messages (conversation_id, sender_id, content, sent_at)
SELECT c.id, 1, 'I need help with task X', now() - interval '5 minutes' FROM c
UNION ALL
SELECT c.id, 3, ':question: **Question** Do you prefer approach A or B?', now() - interval '2 minutes' FROM c;

WITH c AS (SELECT id FROM messaging.conversations WHERE topic_name = '_test-state-4-awaiting-human')
INSERT INTO telemetry.live_events (conversation_id, agent, kind, summary, data, ts)
SELECT c.id, 'product-owner', 'run_start', 'turn started', '{}'::jsonb, now() - interval '3 minutes' FROM c;

WITH c AS (SELECT id FROM messaging.conversations WHERE topic_name = '_test-state-4-awaiting-human')
INSERT INTO messaging.pending_asks (conversation_id, asker_id, question, blocking, asked_at)
SELECT c.id, 3, 'A or B?', true, now() - interval '2 minutes' FROM c;

-- ============================================================
-- CONV 5: ERRORED (run_end with subtype error)
-- ============================================================
WITH c AS (
  INSERT INTO messaging.conversations (stream_id, topic_name, last_message_at)
  VALUES (1, '_test-state-5-errored', now() - interval '3 minutes')
  RETURNING id
)
INSERT INTO messaging.messages (conversation_id, sender_id, content, sent_at)
SELECT c.id, 1, 'try this command that will fail', now() - interval '5 minutes' FROM c;

WITH c AS (SELECT id FROM messaging.conversations WHERE topic_name = '_test-state-5-errored')
INSERT INTO telemetry.live_events (conversation_id, agent, kind, summary, data, ts)
SELECT c.id, 'product-owner', 'run_start', 'turn started', '{}'::jsonb, now() - interval '4 minutes' FROM c
UNION ALL
SELECT c.id, 'product-owner', 'run_end', 'max turns exceeded', '{"subtype":"error_max_turns"}'::jsonb, now() - interval '3 minutes' FROM c;

-- ============================================================
-- CONV 6: PARENT + STUCK CHILD (cascade test)
-- Parent in product-owner; child in executor-legado, same task
-- ============================================================
-- Task to tie parent + child together via origin_topic
INSERT INTO tasks.tasks (slug, title, status)
VALUES ('test-cascade-stuck', 'Test: cascade stuck signal', 'in_progress')
ON CONFLICT (slug) DO UPDATE SET title = EXCLUDED.title, status = 'in_progress';

UPDATE tasks.tasks
   SET origin_stream = 'product-owner',
       origin_topic  = 'task-test-cascade-stuck',
       current_agent = 'executor-legado',
       current_step  = 'execucao',
       status        = 'in_progress'
 WHERE slug = 'test-cascade-stuck';

-- Parent (product-owner)
WITH c AS (
  INSERT INTO messaging.conversations (stream_id, topic_name, last_message_at)
  VALUES (1, 'task-test-cascade-stuck', now() - interval '20 minutes')
  RETURNING id
)
INSERT INTO messaging.messages (conversation_id, sender_id, content, sent_at)
SELECT c.id, 1, 'start the test task', now() - interval '20 minutes' FROM c;

WITH c AS (SELECT id FROM messaging.conversations WHERE topic_name = 'task-test-cascade-stuck' AND stream_id = 1)
INSERT INTO telemetry.live_events (conversation_id, agent, kind, summary, data, ts)
SELECT c.id, 'product-owner', 'run_start', 'turn started', '{}'::jsonb, now() - interval '19 minutes' FROM c
UNION ALL
SELECT c.id, 'product-owner', 'run_end', 'turn complete', '{"subtype":"success"}'::jsonb, now() - interval '17 minutes' FROM c;

-- Child STUCK (executor-legado, same task slug, old run_start)
INSERT INTO messaging.conversations (stream_id, topic_name, last_message_at)
VALUES (4, 'task-test-cascade-stuck', now() - interval '15 minutes');

WITH c AS (SELECT id FROM messaging.conversations WHERE topic_name = 'task-test-cascade-stuck' AND stream_id = 4)
INSERT INTO messaging.messages (conversation_id, sender_id, content, sent_at)
SELECT c.id, 1, 'you can start executing', now() - interval '17 minutes' FROM c;

WITH c AS (SELECT id FROM messaging.conversations WHERE topic_name = 'task-test-cascade-stuck' AND stream_id = 4)
INSERT INTO telemetry.live_events (conversation_id, agent, kind, summary, data, ts)
SELECT c.id, 'executor-legado', 'run_start', 'turn started', '{}'::jsonb, now() - interval '15 minutes' FROM c;

COMMIT;

-- ============================================================
-- VALIDATION QUERY
-- ============================================================
SELECT
  c.topic_name,
  s.name AS stream,
  (SELECT le.kind FROM telemetry.live_events le
    WHERE le.conversation_id = c.id
      AND le.kind IN ('run_start','run_end')
    ORDER BY le.id DESC LIMIT 1) AS last_run_kind,
  (SELECT le.data->>'subtype' FROM telemetry.live_events le
    WHERE le.conversation_id = c.id
      AND le.kind IN ('run_start','run_end')
    ORDER BY le.id DESC LIMIT 1) AS last_run_subtype,
  EXTRACT(EPOCH FROM (now() - (
    SELECT le.ts FROM telemetry.live_events le
     WHERE le.conversation_id = c.id
       AND le.kind IN ('run_start','run_end')
     ORDER BY le.id DESC LIMIT 1
  )))::int AS sec_since_run,
  EXISTS(SELECT 1 FROM messaging.pending_asks pa WHERE pa.conversation_id = c.id AND pa.resolved_at IS NULL) AS pending
 FROM messaging.conversations c
 JOIN messaging.streams s ON s.id = c.stream_id
WHERE c.topic_name LIKE '_test-state-%'
   OR c.topic_name = 'task-test-cascade-stuck'
ORDER BY c.id;
