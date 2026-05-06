-- 003: pg_notify `msg_conv_<conversation_id>` paralelo a `msg_stream_<sid>`
-- e `msg_all`. Motivacao: ask_agent subscrevia ao STREAM inteiro do target
-- (`msg_stream_<target_sid>`), causando o asker a receber todas mensagens
-- posteriores do stream — inclusive perguntas que OUTROS agentes faziam pro
-- target, que o asker interpretava como prompt novo e re-despachava. Com
-- canal per-conversation,
-- o asker escuta so a conv criada pelo seu ask_agent. `msg_stream_*` e
-- `msg_all` preservados pro self-stream do agente (novos topics) e pros
-- consumers de web (SSE + push).

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
