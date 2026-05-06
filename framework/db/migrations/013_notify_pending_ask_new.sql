-- 013: trigger em messaging.pending_asks AFTER INSERT emite pg_notify('ask_new').
--
-- Motivacao (D-77 follow-up): o fluxo de ask_human/ask_agent no bot insere
-- a msg primeiro (commit → msg_all dispara), e *so depois* via HTTP separada
-- cria-se o pending_ask. Isso deixa uma janela em que a SSE entregou o evento
-- de msg pro frontend, que re-fetchou /api/conversations, viu que has_pending_ask
-- ainda era falso, e renderizou sem o badge "NEEDS YOU". F5 mostrava o estado
-- certo porque dai o pending_ask ja existia.
--
-- Com esse trigger, o pending_ask.INSERT dispara um evento proprio que o SSE
-- retransmite. Frontend (coalesceRefresh) deduplica o segundo refresh que
-- chega logo depois do primeiro — custo zero pra caso comum (msg normal sem
-- ask), e corrige o caso de ask_human. Complementa o trigger
-- trg_notify_ask_resolved (UPDATE, ja existente) pra resolve.

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
