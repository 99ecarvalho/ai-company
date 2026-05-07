-- 030: messaging.runs como single source of truth do estado do CLI por
-- conversa. Substitui a derivacao on-the-fly de telemetry.live_events
-- (`is_running` / `runner_state`) que sofria de:
--   * crashes sem run_end deixando "running" pra sempre
--   * eventos perdidos no fire-and-forget POST do runner
--   * 3 fontes independentes (live_events / heartbeat file / pool counter)
--     respondendo a mesma pergunta de forma divergente.
--
-- Modelo: 1 row por execucao do CLI. Status flui:
--   running -> done | error | stale (heartbeat timeout via reaper).
--
-- Producer: o broker (telemetry_live_event handler) faz dual-write na
-- mesma transaction da insert em telemetry.live_events. Heartbeat eh
-- piggyback gratis em cada evento (qualquer kind bate last_heartbeat_at).
--
-- Idempotencia: partial unique index garante no maximo 1 row 'running'
-- por conversation. Re-emissao de run_start bate o heartbeat em vez de
-- duplicar. Se o runner crasha + recupera com --resume, o reaper
-- ja transicionou pra 'stale' antes do novo spawn → INSERT sem conflito.

CREATE TABLE messaging.runs (
    id                 BIGSERIAL PRIMARY KEY,
    conversation_id    INTEGER NOT NULL REFERENCES messaging.conversations(id) ON DELETE CASCADE,
    agent              TEXT NOT NULL,
    topic_slug         TEXT NOT NULL,
    session_id         TEXT,
    status             TEXT NOT NULL CHECK (status IN ('running', 'done', 'error', 'stale')),
    started_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_heartbeat_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at        TIMESTAMPTZ,
    exit_reason        TEXT,
    metadata           JSONB
);

-- Lookup principal: ultimo run por conversa (LATERAL join no broker).
CREATE INDEX runs_conv_started_idx
    ON messaging.runs (conversation_id, started_at DESC);

-- Reaper varre runs ativas com heartbeat antigo.
CREATE INDEX runs_running_heartbeat_idx
    ON messaging.runs (last_heartbeat_at)
    WHERE status = 'running';

-- Idempotencia: re-INSERT de run_start pra mesma conv vira UPDATE de
-- heartbeat via ON CONFLICT. Predicate WHERE status='running' permite
-- multiplas rows historicas (done/error/stale) por conv.
CREATE UNIQUE INDEX runs_one_running_per_conv_idx
    ON messaging.runs (conversation_id)
    WHERE status = 'running';

-- O trigger existente em 015_notify_conv_activity.sql ja emite
-- 'conv_activity' em todo INSERT em telemetry.live_events, o que cobre
-- as transicoes running → done/error (que sao acompanhadas de live_event
-- run_end). A unica transicao que NAO tem live_event correspondente eh
-- running → stale, feita pelo reaper. Trigger abaixo cobre esse caso.
CREATE OR REPLACE FUNCTION messaging.notify_runs_stale() RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status = 'running' AND NEW.status = 'stale' THEN
        PERFORM pg_notify(
            'conv_activity',
            json_build_object(
                'conversation_id', NEW.conversation_id,
                'kind', 'run_stale',
                'ts', extract(epoch from now())
            )::text
        );
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_notify_runs_stale
    AFTER UPDATE ON messaging.runs
    FOR EACH ROW EXECUTE FUNCTION messaging.notify_runs_stale();
