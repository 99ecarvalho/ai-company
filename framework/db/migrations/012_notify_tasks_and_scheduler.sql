-- 012: pg_notify em tasks.tasks (INSERT/UPDATE) pra SSE `/api/tasks/events`
-- (substitui polling de 8s no PWA). Payload slim dentro do limite 8KB —
-- clientes fazem refetch da lista full como eh autoritativo pra contagens
-- derivadas, phases_count, archived etc.
--
-- `scheduler_event` nao tem trigger SQL — eh emitido direto pelo APScheduler
-- no container scheduler via pg_notify explicito (job events sao memoria do
-- APScheduler, nao UPDATE em tabela). So reservamos o channel name aqui como
-- doc.

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

-- Documentacao do channel emitido pelo scheduler (sem trigger SQL).
COMMENT ON FUNCTION tasks.notify_task_changed() IS
  'Emits pg_notify(task_changed) on tasks.tasks INSERT/UPDATE; consumed by web SSE /api/tasks/events';
