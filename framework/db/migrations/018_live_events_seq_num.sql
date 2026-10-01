-- 018: seq_num on telemetry.live_events
--
-- Context: claude_runner emits thinking/tool_use via asyncio.create_task
-- (fire-and-forget) within the same assistant message. The concurrent POSTs
-- reach the DB in non-deterministic order. `id` BIGSERIAL is assigned
-- in DB commit order, not in the logical order of the SDK stream, so
-- thinking can have a HIGHER id than the tool_use that logically came after
-- it. The frontend sorts by `ts, id`, but when they fall in the same ts
-- second (current resolution), the id tie-break reflects the race, not the
-- model's order.
--
-- Fix: `seq_num` generated in the agent in strict order per conversation, via
-- asyncio.Lock + local counter. Monotonically increasing per conv.
-- The frontend tie-breaks by `seq_num` before id.
--
-- Backfill: `seq_num = id` for old rows (best approximation — uses
-- INSERT order as the historical fallback).

ALTER TABLE telemetry.live_events
    ADD COLUMN seq_num BIGINT;

UPDATE telemetry.live_events SET seq_num = id WHERE seq_num IS NULL;

-- Composite index for "all events of a conv in order" queries:
-- complements the current index (conversation_id, ts DESC) that serves
-- descending reads. The primary tie-break is still ts; seq_num only matters
-- for breaking ties.
CREATE INDEX idx_live_events_conv_seq ON telemetry.live_events (conversation_id, seq_num);

-- Include seq_num in the NOTIFY payload so the PWA gets the correct
-- tie-break without needing a refetch.
CREATE OR REPLACE FUNCTION telemetry.notify_live_event() RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify(
        'live_event_' || NEW.conversation_id,
        json_build_object(
            'id', NEW.id,
            'seq_num', NEW.seq_num,
            'agent', NEW.agent,
            'ts', extract(epoch from NEW.ts),
            'kind', NEW.kind,
            'summary', NEW.summary
        )::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
