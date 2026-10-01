-- 024: claude_session_cwd on messaging.conversations.
--
-- Claude CLI stores `<session_id>.jsonl` in ~/.claude/projects/<encoded-cwd>/.
-- The project directory is per-cwd: the same session_id is only readable by
-- `claude --resume` when spawn_cwd matches the cwd that created it.
--
-- Before this column, the runner persisted only `claude_session_id`. When
-- `_resolve_spawn_cwd` returned a different cwd between runs of the same topic
-- (e.g. the agent picked up a task in parallel and got a worktree, then released it),
-- `--resume` went to a project dir that lacked that id's jsonl —
-- ghost session, recovery wiped the context. Persisting the cwd lets the
-- runner proactively skip `--resume` when the current cwd differs from the
-- recorded one, keeping the sid for future runs that return to the same cwd.
--
-- Additive: NULL default (legacy: sids stored pre-migration have no cwd
-- recorded, and the runner treats it as "no bound cwd, accept any").
-- Reversible via DROP COLUMN claude_session_cwd.

ALTER TABLE messaging.conversations
  ADD COLUMN claude_session_cwd TEXT;
