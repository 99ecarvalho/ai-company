-- 030: messaging.runs as the single source of truth for CLI state per
-- conversation. Replaces the on-the-fly derivation from telemetry.live_events
-- (`is_running` / `runner_state`), which suffered from:
--   * crashes without run_end leaving "running" forever
--   * events lost in the runner's fire-and-forget POST
--   * 3 independent sources (live_events / heartbeat file / pool counter)
--     answering the same question inconsistently.
--
-- Model: 1 row per CLI run. Status flows:
--   running -> done | error | stale (heartbeat timeout via reaper).
--
-- Producer: the broker (telemetry_live_event handler) dual-writes in the
-- same transaction as the insert into telemetry.live_events. Heartbeat is a
-- free piggyback on every event (any kind bumps last_heartbeat_at).
--
-- Idempotency: a partial unique index guarantees at most 1 'running' row
-- per conversation. Re-emitting run_start bumps the heartbeat instead of
-- duplicating. If the runner crashes + recovers with --resume, the reaper
-- has already moved it to 'stale' before the new spawn → INSERT without conflict.

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

-- Main lookup: latest run per conversation (LATERAL join in the broker).
CREATE INDEX runs_conv_started_idx
    ON messaging.runs (conversation_id, started_at DESC);

-- The reaper scans active runs with an old heartbeat.
CREATE INDEX runs_running_heartbeat_idx
    ON messaging.runs (last_heartbeat_at)
    WHERE status = 'running';

-- Idempotency: re-INSERT of run_start for the same conv becomes a heartbeat
-- UPDATE via ON CONFLICT. The WHERE status='running' predicate allows
-- multiple historical rows (done/error/stale) per conv.
CREATE UNIQUE INDEX runs_one_running_per_conv_idx
    ON messaging.runs (conversation_id)
    WHERE status = 'running';

-- The existing trigger in 015_notify_conv_activity.sql already emits
-- 'conv_activity' on every INSERT into telemetry.live_events, which covers
-- the running → done/error transitions (which come with a run_end
-- live_event). The only transition WITHOUT a matching live_event is
-- running → stale, done by the reaper. The trigger below covers that case.
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
