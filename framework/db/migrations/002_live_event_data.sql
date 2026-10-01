-- 002: include `data` in the live_event pg_notify payload, so the
-- frontend can aggregate cost/duration/num_turns (structured fields in
-- run_end) in the topic header without a second roundtrip. Default '{}' for
-- compatibility with old events (which stored `data=NULL`).

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
            'data', COALESCE(NEW.data, '{}'::jsonb)
        )::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
