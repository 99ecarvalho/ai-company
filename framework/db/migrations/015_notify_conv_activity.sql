-- 015: emite pg_notify `conv_activity` quando telemetry.live_events recebe
-- um `run_start` ou `run_end`. D-81.
--
-- Motivo: o campo `is_queued` em /api/conversations (D-76/D-78) deriva de
-- `last_trigger_at > last_run_activity_at`. Quando o pool libera e o
-- claude_runner spawna, um novo `run_start` é INSERTed em live_events —
-- mas o SSE global /api/events só escuta `msg_all` e `ask_new`, não
-- live_events. Resultado: a sidebar continua mostrando `QUEUED` mesmo
-- depois do runner começar a rodar (tool_uses aparecem no chat mas o
-- card não atualiza), até a próxima msg chegar.
--
-- Fix: trigger nova dispara pg_notify no canal `conv_activity` com
-- payload `{conversation_id, kind, ts}` sempre que vier run_start ou
-- run_end. Frontend re-fetcha /api/conversations ao receber essa
-- notificação — is_queued fica sincronizado com runner real.
--
-- Canal separado de `live_event_<conv_id>` pra não quebrar consumers
-- que só querem eventos da conv ativa (SSE do chat). Canal separado
-- de `msg_all` pra não confundir consumers que esperam payload de msg.

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
