-- 016: DB-backed scheduler — custom_jobs CRUD + per-instance native_overrides.
--
-- Motivation. Until now, every scheduler job came from YAML
-- (`framework/orchestrator/defaults/schedule.yaml` + `agents/schedule.yaml`).
-- Creating/editing required an edit + `docker compose restart scheduler`. Native
-- defaults (backup, cleanup, cost_budget_check) and custom ones (post_message)
-- were mixed in the same YAML, with no separation.
--
-- New model:
--   - `scheduler.custom_jobs`: jobs created via PWA/MCP. Source of truth
--     for custom jobs. Hot-reload via pg_notify('scheduler_config_reload').
--   - `scheduler.native_overrides`: per-instance override of native jobs
--     (which keep their defaults in the framework YAML). cron_override=NULL
--     means "use default"; enabled=false disables the job at runtime.
--
-- The `scheduler_config_reload` channel is emitted by the write endpoints
-- (scheduler_routes.py) and by the MCP handler — scheduler LISTENs + reschedules
-- via APScheduler.add_job(..., replace_existing=True) / remove_job.

CREATE SCHEMA IF NOT EXISTS scheduler;

CREATE TABLE IF NOT EXISTS scheduler.custom_jobs (
    slug TEXT PRIMARY KEY,
    cron TEXT NOT NULL,
    action TEXT NOT NULL,
    params JSONB NOT NULL DEFAULT '{}'::jsonb,
    description TEXT,
    enabled BOOLEAN NOT NULL DEFAULT true,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS scheduler.native_overrides (
    id TEXT PRIMARY KEY,                    -- matches framework default YAML job id
    cron_override TEXT,                     -- NULL = use framework default
    enabled BOOLEAN NOT NULL DEFAULT true,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION scheduler.touch_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_custom_jobs_touch ON scheduler.custom_jobs;
CREATE TRIGGER trg_custom_jobs_touch BEFORE UPDATE ON scheduler.custom_jobs
    FOR EACH ROW EXECUTE FUNCTION scheduler.touch_updated_at();

DROP TRIGGER IF EXISTS trg_native_overrides_touch ON scheduler.native_overrides;
CREATE TRIGGER trg_native_overrides_touch BEFORE UPDATE ON scheduler.native_overrides
    FOR EACH ROW EXECUTE FUNCTION scheduler.touch_updated_at();
