-- 017: explicit parent_conv_id on messaging.conversations (D-87).
--
-- Reason: the parent-child hierarchy was inferred heuristically in
-- _compute_hierarchy (broker.py) — parsing the `__ask-from-CHAIN-uid` topic
-- + correlation with the ask_agent tool_use in telemetry.live_events. Fragile
-- for multi-level (2+ level chains gave the wrong parent_agent) and
-- needs synthetic events (D-86) for inline subagents. The "once and for
-- all" solution: persist parent_conv_id directly in the table
-- when the conv is created. Agent/reactor pass the asker_conv_id
-- explicitly; the broker stores it. Hierarchy becomes a trivial O(1) lookup.
--
-- Existing convs (pre-migration) stay NULL — the old heuristic
-- remains as a fallback. Optional backfill via a separate script.

ALTER TABLE messaging.conversations
  ADD COLUMN parent_conv_id INTEGER NULL
  REFERENCES messaging.conversations(id) ON DELETE SET NULL;

CREATE INDEX idx_conversations_parent
  ON messaging.conversations(parent_conv_id)
  WHERE parent_conv_id IS NOT NULL;
