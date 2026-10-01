-- 009: claude_session_id on messaging.conversations to allow `claude --resume`
-- after an agent restart, without depending on filesystem state.
--
-- Replaces `instance/sessions/<agent>/<topic>/.session_state.json` (old
-- SessionManager). Reason: session_id is the only state that must survive a restart in
-- sessions_root; moving it to the DB removes the filesystem dependency and unifies it with
-- memory/tasks/backlog (all DB). SessionManager now reads/writes via the DB pool.
--
-- Additive: NULL default preserves current behavior (all convs without a registered
-- session_id). Trivially reversible (DROP COLUMN claude_session_id,
-- claude_session_used_at).

ALTER TABLE messaging.conversations
  ADD COLUMN claude_session_id      TEXT,
  ADD COLUMN claude_session_used_at TIMESTAMPTZ;
