-- 008: archived_at on messaging.conversations for manual soft-delete of threads
-- by the human in the PWA.
--
-- Part of the D-57 plan (tasks <-> threads unification + manual Active/Closed):
-- the PWA now has *one* thread list (kills the Mine/Background/Tasks tabs),
-- with two filters, Active (archived_at IS NULL) and Closed (archived_at IS NOT
-- NULL). Closing/reopening is always the human's decision, never automatic —
-- not even when the linked task hits a terminal (done/halt/human_review) does the
-- thread close by itself.
--
-- Additive: NULL default preserves current behavior (all existing convs
-- become Active). Trivially reversible (DROP COLUMN).

ALTER TABLE messaging.conversations
  ADD COLUMN archived_at TIMESTAMPTZ;

CREATE INDEX conversations_archived_idx
  ON messaging.conversations (archived_at)
  WHERE archived_at IS NOT NULL;
