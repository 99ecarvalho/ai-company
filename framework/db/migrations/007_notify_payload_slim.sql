-- 007: remove `content` e `sent_at` do payload do pg_notify em
-- messaging.notify_message. Postgres limita o payload do NOTIFY a 8000 bytes
-- (hardcoded, compile-time) — qualquer mensagem cujo JSON inteiro passasse
-- desse limite causava `InvalidParameterValueError: payload string too long`,
-- abortando o INSERT inteiro (trigger fora da transacao do caller) e perdendo
-- a mensagem. Sintoma visto em 2026-04-22: resposta de 9128 bytes do
-- investigador-producao falhou no reply com HTTP 500, nunca persistida.
--
-- Fix: payload minimo (id + conversation_id + sender_id). Consumers que
-- precisam de content/sent_at buscam por `SELECT ... WHERE id = $1` no
-- callback — custo desprezivel (index lookup) e mata o modo de falha.
--
-- Consumers atuais (todos ja compativeis ou ajustados neste commit):
--   - bots/internal_client._on_notify → ja fazia HTTP enrich via id
--   - web/broker.events_sse → SELECT enrich atualizado pra incluir content/sent_at
--   - web/main._push_notifier_loop → SELECT enrich atualizado pra incluir content

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
