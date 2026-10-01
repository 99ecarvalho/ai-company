-- 015: emits pg_notify `conv_activity` when telemetry.live_events receives
-- a `run_start` or `run_end`. D-81.
--
-- Reason: the `is_queued` field in /api/conversations (D-76/D-78) is derived from
-- `last_trigger_at > last_run_activity_at`. When the pool frees up and
-- claude_runner spawns, a new `run_start` is INSERTed into live_events —
-- but the global SSE /api/events only listens to `msg_all` and `ask_new`, not
-- live_events. Result: the sidebar keeps showing `QUEUED` even
-- after the runner starts (tool_uses show up in the chat but the
-- card does not update), until the next msg arrives.
--
-- Fix: a new trigger fires pg_notify on the `conv_activity` channel with
-- payload `{conversation_id, kind, ts}` whenever a run_start or
-- run_end arrives. The frontend re-fetches /api/conversations on receiving this
-- notification — is_queued stays in sync with the real runner.
--
-- Separate channel from `live_event_<conv_id>` so as not to break consumers
-- that only want events of the active conv (chat SSE). Separate channel
-- from `msg_all` so as not to confuse consumers that expect a msg payload.

CREATE OR REPLACE FUNCTION telemetry.notify_conv_activity() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.kind IN ('run_start', 'run_end') THEN
        PERFORM pg_notify(
            'conv_activity',
            json_build_object(
                'conversation_id', NEW.conversation_id,
                'kind', NEW.kind,
                'ts', extract(epoch from NEW.ts)
            )::text
        );
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_notify_conv_activity AFTER INSERT ON telemetry.live_events
FOR EACH ROW EXECUTE FUNCTION telemetry.notify_conv_activity();
