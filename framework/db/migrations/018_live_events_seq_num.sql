-- 018: seq_num em telemetry.live_events
--
-- Contexto: claude_runner emite thinking/tool_use via asyncio.create_task
-- (fire-and-forget) no mesmo assistant message. Os POSTs concorrentes
-- chegam ao banco em ordem nao-deterministica. `id` BIGSERIAL e atribuido
-- na ordem de commit no banco, nao na ordem logica do stream do SDK, entao
-- thinking pode ter id MAIOR que o tool_use que logicamente veio depois
-- dele. Frontend ordena por `ts, id` mas quando caem no mesmo segundo
-- ts (resolucao atual), o tie-break por id reflete a corrida, nao a ordem
-- do modelo.
--
-- Fix: `seq_num` gerado no agente em ordem estrita por conversation, via
-- asyncio.Lock + contador local. Monotonicamente crescente por conv.
-- Frontend tie-breaks por `seq_num` antes de id.
--
-- Backfill: `seq_num = id` pra linhas antigas (melhor aproximacao — usa
-- a ordem de INSERT como fallback histórico).

ALTER TABLE telemetry.live_events
    ADD COLUMN seq_num BIGINT;

UPDATE telemetry.live_events SET seq_num = id WHERE seq_num IS NULL;

-- Index composto pra queries "todos eventos de uma conv em ordem":
-- complementar ao index atual (conversation_id, ts DESC) que serve leitura
-- descendente. O tie-break primario continua por ts; seq_num so importa
-- no desempate.
CREATE INDEX idx_live_events_conv_seq ON telemetry.live_events (conversation_id, seq_num);

-- Incluir seq_num no payload do NOTIFY pra o PWA ja receber o tie-break
-- correto sem precisar de refetch.
CREATE OR REPLACE FUNCTION telemetry.notify_live_event() RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify(
        'live_event_' || NEW.conversation_id,
        json_build_object(
            'id', NEW.id,
            'seq_num', NEW.seq_num,
            'agent', NEW.agent,
            'ts', extract(epoch from NEW.ts),
            'kind', NEW.kind,
            'summary', NEW.summary
        )::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
