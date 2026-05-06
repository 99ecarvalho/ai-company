-- 014: trigger telemetry.notify_live_event filtra `output_full` do payload
-- do pg_notify. Motivo: tool_result com saida grande (queries SQL retornando
-- muitas linhas, stdout de testes, etc) precisa ficar acessivel via "View
-- full" no PWA, mas o pg_notify tem limite de 8000 bytes — emitir o
-- output inteiro estoura silenciosamente. Solucao:
--   - claude_runner armazena 5KB inline em `data.output` + cap mais
--     generoso (~200KB) em `data.output_full`.
--   - Este trigger emite NOTIFY com data SEM `output_full` (jsonb -
--     'output_full'), garantindo payload pequeno.
--   - Frontend, ao detectar `output_truncated=true`, busca o full via
--     GET /api/live_events/{id}/full (le do JSONB no banco).

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
