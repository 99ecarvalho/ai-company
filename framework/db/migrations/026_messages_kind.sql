-- 026_messages_kind.sql
-- Distinguishes "regular" messages from "echo" messages (D-100 forwarded reply).
--
-- Problem: the D-100 echo posts a copy of the child's reply into the parent conv for
-- human visibility — a human viewing the parent conv (the PO's task-conv) can
-- read what the child answered without opening the child conv. But the copy
-- goes out as a regular messaging.messages row, fires `pg_notify('msg_stream_<sid>')`
-- and wakes the parent agent's runner for an extra turn. In a conv used as
-- an ask_agent destination (asker subscribe + sync wait via MCP), the parent agent
-- already got the answer synchronously and does not need to "see it again" — the
-- echo then produces a redundant duplicate turn.
--
-- Architectural fix: echo is a signal for the human, not for the agent. A `kind` column
-- on messages distinguishes 'regular' (produces a turn) from 'echo' (purely
-- visual). The trigger includes kind in the NOTIFY payload; ai_company filters
-- and skips dispatch when kind != 'regular'. PWA SSE keeps receiving
-- all of them (same `msg_all` channel) — the human sees them as usual.
--
-- Set: regular | echo. Default 'regular' preserves legacy behavior
-- for every existing INSERT (including reactor handoff, ask_human
-- reply, etc). Only claude_runner._reply in the D-100 branch marks 'echo'.

ALTER TABLE messaging.messages
    ADD COLUMN kind TEXT NOT NULL DEFAULT 'regular'
        CHECK (kind IN ('regular', 'echo'));

COMMENT ON COLUMN messaging.messages.kind IS
    'regular = message that triggers a turn in the agent listener; '
    'echo = D-100 visual forward of a child conversation reply to the parent (skips '
    'dispatch in the listener but stays visible to the human).';

-- Updated trigger: includes `kind` in the pg_notify payload so the
-- listener can filter without an extra SELECT. Keeps the same channels
-- (msg_all / msg_stream_<sid> / msg_conv_<id>).
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
        'kind', NEW.kind
    );
    PERFORM pg_notify('msg_conv_' || NEW.conversation_id, payload::text);
    PERFORM pg_notify('msg_stream_' || stream_id, payload::text);
    PERFORM pg_notify('msg_all', payload::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
