-- 007: remove `content` and `sent_at` from the pg_notify payload in
-- messaging.notify_message. Postgres limits the NOTIFY payload to 8000 bytes
-- (hardcoded, compile-time) — any message whose full JSON exceeded
-- that limit caused `InvalidParameterValueError: payload string too long`,
-- aborting the whole INSERT (trigger outside the caller's transaction) and losing
-- the message. Symptom seen on 2026-04-22: a 9128-byte reply from
-- investigador-producao failed with HTTP 500, never persisted.
--
-- Fix: minimal payload (id + conversation_id + sender_id). Consumers that
-- need content/sent_at fetch it with `SELECT ... WHERE id = $1` in the
-- callback — negligible cost (index lookup) and it kills the failure mode.
--
-- Current consumers (all already compatible or adjusted in this commit):
--   - bots/internal_client._on_notify → already did HTTP enrich via id
--   - web/broker.events_sse → SELECT enrich updated to include content/sent_at
--   - web/main._push_notifier_loop → SELECT enrich updated to include content

CREATE OR REPLACE FUNCTION messaging.notify_message() RETURNS TRIGGER AS $$
DECLARE
    stream_id INTEGER;
    payload JSONB;
BEGIN
    SELECT c.stream_id INTO stream_id FROM messaging.conversations c WHERE c.id = NEW.conversation_id;
    payload = jsonb_build_object(
        'id', NEW.id,
        'conversation_id', NEW.conversation_id,
        'sender_id', NEW.sender_id
    );
    PERFORM pg_notify('msg_conv_' || NEW.conversation_id, payload::text);
    PERFORM pg_notify('msg_stream_' || stream_id, payload::text);
    PERFORM pg_notify('msg_all', payload::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
