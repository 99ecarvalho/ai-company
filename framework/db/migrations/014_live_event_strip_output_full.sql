-- 014: trigger telemetry.notify_live_event strips `output_full` from the
-- pg_notify payload. Reason: tool_result with large output (SQL queries returning
-- many rows, test stdout, etc) must stay accessible via "View
-- full" in the PWA, but pg_notify has an 8000-byte limit — emitting the
-- whole output overflows silently. Solution:
--   - claude_runner stores 5KB inline in `data.output` + a more
--     generous cap (~200KB) in `data.output_full`.
--   - This trigger emits NOTIFY with data WITHOUT `output_full` (jsonb -
--     'output_full'), guaranteeing a small payload.
--   - The frontend, on detecting `output_truncated=true`, fetches the full one via
--     GET /api/live_events/{id}/full (reads the JSONB from the DB).

CREATE OR REPLACE FUNCTION telemetry.notify_live_event() RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify(
        'live_event_' || NEW.conversation_id,
        json_build_object(
            'id', NEW.id,
            'agent', NEW.agent,
            'ts', extract(epoch from NEW.ts),
            'kind', NEW.kind,
            'summary', NEW.summary,
            'data', COALESCE(NEW.data, '{}'::jsonb) - 'output_full'
        )::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
