-- 013: AFTER INSERT trigger on messaging.pending_asks emits pg_notify('ask_new').
--
-- Motivation (D-77 follow-up): the ask_human/ask_agent flow in the bot inserts
-- the msg first (commit → msg_all fires), and *only afterwards*, via a separate HTTP call,
-- creates the pending_ask. That leaves a window where SSE delivered the msg
-- event to the frontend, which re-fetched /api/conversations, saw has_pending_ask
-- still false, and rendered without the "NEEDS YOU" badge. F5 showed the right
-- state because by then the pending_ask existed.
--
-- With this trigger, pending_ask.INSERT fires its own event that SSE
-- relays. The frontend (coalesceRefresh) dedups the second refresh that
-- arrives right after the first — zero cost for the common case (normal msg without
-- ask), and it fixes the ask_human case. Complements the
-- trg_notify_ask_resolved trigger (UPDATE, already existing) for resolve.

CREATE OR REPLACE FUNCTION messaging.notify_pending_ask_new() RETURNS TRIGGER AS $$
DECLARE
    payload JSONB;
BEGIN
    payload = jsonb_build_object(
        'conversation_id', NEW.conversation_id,
        'asker_id', NEW.asker_id,
        'kind', 'ask_new'
    );
    PERFORM pg_notify('ask_new', payload::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_notify_pending_ask_new ON messaging.pending_asks;
CREATE TRIGGER trg_notify_pending_ask_new
    AFTER INSERT ON messaging.pending_asks
    FOR EACH ROW EXECUTE FUNCTION messaging.notify_pending_ask_new();
