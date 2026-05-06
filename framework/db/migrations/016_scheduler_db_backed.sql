-- 016: scheduler DB-backed — custom_jobs CRUD + native_overrides por instancia.
--
-- Motivacao. Ate aqui, todo job do scheduler vinha do YAML
-- (`framework/orchestrator/defaults/schedule.yaml` + `agents/schedule.yaml`).
-- Criar/editar exigia edit + `docker compose restart scheduler`. Native
-- defaults (backup, cleanup, cost_budget_check) e custom (post_message)
-- ficavam misturados no mesmo YAML, sem separacao.
--
-- Nova modelagem:
--   - `scheduler.custom_jobs`: jobs criados via PWA/MCP. Fonte de verdade
--     pros custom. Hot-reload via pg_notify('scheduler_config_reload').
--   - `scheduler.native_overrides`: override por instancia dos jobs nativos
--     (que continuam com defaults no framework YAML). cron_override=NULL
--     significa "use default"; enabled=false desativa o job em runtime.
--
-- O canal `scheduler_config_reload` eh emitido pelos endpoints de write
-- (scheduler_routes.py) e pelo MCP handler — scheduler LISTEN + reschedule
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
