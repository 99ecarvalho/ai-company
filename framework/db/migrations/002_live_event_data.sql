-- 002: inclui `data` no payload do pg_notify do live_event, pra que o
-- frontend possa agregar cost/duration/num_turns (campos estruturados em
-- run_end) no header do topic sem segundo roundtrip. Default '{}' pra
-- compatibilidade com events antigos (que guardavam `data=NULL`).

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
