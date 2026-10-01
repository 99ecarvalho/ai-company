-- 027_conversations_custom_title.sql
-- Per-conversation customizable title, independent of topic_name (which is the
-- stable ID of the conversation in the broker and usually reflects an operational pattern:
-- 'task-<slug>', '__ask-from-X-Y', '__child-...', etc).
--
-- Problem: the human looks at the sidebar to understand which conversations are
-- active, but topic_name is often technical/repetitive (several
-- 'task-fix-...' side by side). `task.title` already helps when there is a linked
-- task, but for free-form conversations or to give a contextual nickname,
-- the human needs a field of their own.
--
-- Decision: nullable `custom_title` column on messaging.conversations.
-- Frontend display rule: `custom_title || task.title || topic`. Editable
-- inline in the sidebar. NULL = no override (falls back). The initial backfill
-- copies topic_name into all existing conversations — so when the
-- human clicks to edit for the first time they already see the current value in the input
-- (instead of an empty one). New conversations start as NULL and the
-- fallback rule covers them.

ALTER TABLE messaging.conversations
    ADD COLUMN custom_title TEXT;

COMMENT ON COLUMN messaging.conversations.custom_title IS
    'Title the human can customize in the PWA. NULL = use the fallback '
    '(task.title || topic_name). Editable inline in the sidebar.';

UPDATE messaging.conversations
   SET custom_title = topic_name
 WHERE custom_title IS NULL;
