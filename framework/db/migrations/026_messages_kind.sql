-- 026_messages_kind.sql
-- Distingue mensagens "regular" de mensagens "echo" (D-100 forwarded reply).
--
-- Problema: o D-100 echo posta a copia do reply do filho na conv pai pra
-- humanidade visual — humano vendo a conv pai (task-conv do PO) consegue
-- ler o que o filho respondeu sem precisar abrir a conv-filha. Mas a copia
-- vai como messaging.messages comum, dispara `pg_notify('msg_stream_<sid>')`
-- e acorda o runner do agente pai pra um turno extra. Em conv usada como
-- destino de ask_agent (asker subscribe + sync wait via MCP), o agente pai
-- ja recebeu a resposta sincronicamente e nao precisa "ver de novo" — o
-- echo entao gera um turn duplicado redundante.
--
-- Fix arquitetural: echo eh sinal pro humano, nao pro agente. Coluna `kind`
-- nas messages distingue 'regular' (gera turn) de 'echo' (puramente
-- visual). Trigger inclui kind no payload do NOTIFY; ai_company filtra
-- e ignora dispatch quando kind != 'regular'. PWA SSE continua recebendo
-- todas (mesmo `msg_all` channel) — humano enxerga normal.
--
-- Conjunto: regular | echo. Default 'regular' preserva comportamento
-- legado pra todo INSERT existente (incluindo handoff do reactor, ask_human
-- reply, etc). Apenas claude_runner._reply na branch D-100 marca 'echo'.

ALTER TABLE messaging.messages
    ADD COLUMN kind TEXT NOT NULL DEFAULT 'regular'
        CHECK (kind IN ('regular', 'echo'));

COMMENT ON COLUMN messaging.messages.kind IS
    'regular = mensagem que dispara turn no listener do agente; '
    'echo = D-100 forward visual do reply de conv-filha pra parent (pula '
    'dispatch no listener mas continua visivel pro humano).';

-- Trigger atualizado: inclui `kind` no payload do pg_notify pra que o
-- listener filtre sem precisar fazer SELECT extra. Mantem mesmos channels
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
