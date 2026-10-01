-- 003: pg_notify `msg_conv_<conversation_id>` alongside `msg_stream_<sid>`
-- and `msg_all`. Motivation: ask_agent subscribed to the target's ENTIRE stream
-- (`msg_stream_<target_sid>`), so the asker received all later messages
-- on the stream — including questions OTHER agents asked the
-- target, which the asker read as a new prompt and re-dispatched. With a
-- per-conversation channel,
-- the asker listens only to the conv created by its ask_agent. `msg_stream_*` and
-- `msg_all` are kept for the agent's self-stream (new topics) and for the
-- web consumers (SSE + push).

CREATE OR REPLACE FUNCTION messaging.notify_message() RETURNS TRIGGER AS $$
DECLARE
    stream_id INTEGER;
    payload JSONB;
BEGIN
    SELECT c.stream_id INTO stream_id FROM messaging.conversations c WHERE c.id = NEW.conversation_id;
    payload = jsonb_build_object(
        'id', NEW.id,
        'conversation_id', NEW.conversation_id,
        'sender_id', NEW.sender_id,
        'content', NEW.content,
        'sent_at', NEW.sent_at
    );
    PERFORM pg_notify('msg_conv_' || NEW.conversation_id, payload::text);
    PERFORM pg_notify('msg_stream_' || stream_id, payload::text);
    PERFORM pg_notify('msg_all', payload::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
