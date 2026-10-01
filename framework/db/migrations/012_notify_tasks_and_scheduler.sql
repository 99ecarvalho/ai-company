-- 012: pg_notify on tasks.tasks (INSERT/UPDATE) for SSE `/api/tasks/events`
-- (replaces 8s polling in the PWA). Slim payload within the 8KB limit —
-- clients refetch the full list since it is authoritative for derived
-- counts, phases_count, archived etc.
--
-- `scheduler_event` has no SQL trigger — it is emitted directly by APScheduler
-- in the scheduler container via an explicit pg_notify (job events live in
-- APScheduler memory, not a table UPDATE). We only reserve the channel name here as
-- documentation.

CREATE OR REPLACE FUNCTION tasks.notify_task_changed() RETURNS TRIGGER AS $$
DECLARE
    payload JSONB;
BEGIN
    payload = jsonb_build_object(
        'slug', NEW.slug,
        'status', NEW.status,
        'current_agent', NEW.current_agent,
        'archived', NEW.archived_at IS NOT NULL,
        'updated_at', extract(epoch from NEW.updated_at)
    );
    PERFORM pg_notify('task_changed', payload::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tasks_tasks_notify ON tasks.tasks;
CREATE TRIGGER tasks_tasks_notify
    AFTER INSERT OR UPDATE ON tasks.tasks
    FOR EACH ROW EXECUTE FUNCTION tasks.notify_task_changed();

-- Documentation of the channel emitted by the scheduler (no SQL trigger).
COMMENT ON FUNCTION tasks.notify_task_changed() IS
  'Emits pg_notify(task_changed) on tasks.tasks INSERT/UPDATE; consumed by web SSE /api/tasks/events';
