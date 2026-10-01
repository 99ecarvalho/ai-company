-- 010: reverts the sum that 004 applied to telemetry.events.input_tokens
--
-- Context: migration 004 and claude_runner started storing
-- `input_tokens = base + cache_creation + cache_read` so the PWA "IN" would
-- reflect the total billed. In practice this created two distortions:
--   1. The name clashes with the Anthropic API semantics, where `input_tokens`
--      means ONLY non-cached tokens (the other two counters are
--      disjoint and have their own price).
--   2. Since cache dominates >99% in most runs, the IN column and the
--      cache column look identical in the PWA, hiding how much is really new
--      input.
--
-- From now on claude_runner stores only the base (non-cached) in
-- input_tokens, and the PWA can show the 3 counters separately when it wants
-- the sum. This backfill restores existing records to the same format.
--
-- Safe: GREATEST(..., 0) covers any rows where the arithmetic would not
-- come out exact (rare, due to CLI rounding). NULL cache stays
-- NULL and input_tokens is left untouched.

UPDATE telemetry.events
   SET input_tokens = GREATEST(
         input_tokens
           - COALESCE(cache_creation_tokens, 0)
           - COALESCE(cache_read_tokens, 0),
         0)
 WHERE input_tokens IS NOT NULL
   AND (cache_creation_tokens IS NOT NULL OR cache_read_tokens IS NOT NULL);
