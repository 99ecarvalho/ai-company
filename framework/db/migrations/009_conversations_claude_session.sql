-- 009: claude_session_id em messaging.conversations pra permitir `claude --resume`
-- apos restart do agente, sem depender de filesystem state.
--
-- Substitui `instance/sessions/<agent>/<topic>/.session_state.json` (SessionManager
-- antigo). Motivo: session_id e o unico state que precisa sobreviver a restart em
-- sessions_root; migrar pro banco elimina dependencia de filesystem e unifica com
-- memoria/tasks/backlog (todos DB). SessionManager passa a ler/gravar via DB pool.
--
-- Aditivo: NULL default preserva comportamento atual (todas as convs sem session_id
-- registrado). Reversivel trivialmente (DROP COLUMN claude_session_id,
-- claude_session_used_at).

ALTER TABLE messaging.conversations
  ADD COLUMN claude_session_id      TEXT,
  ADD COLUMN claude_session_used_at TIMESTAMPTZ;
