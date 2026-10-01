-- 004_telemetry_cache_tokens.sql
-- Adds dedicated columns for cache tokens in telemetry.events and backfills
-- from the JSONB metadata (where claude_runner currently stuffs cache_*).
-- Context: the historical `input_tokens` column holds only NON-cached tokens
-- (what the Claude CLI calls `input_tokens` in usage). Cache creation/read
-- lived only in `metadata.cache_creation_tokens` / `metadata.cache_read_tokens`
-- and never made it into the summary — PWA showed a fake IN (much lower than real).
-- From now on claude_runner sends the 3 fields separately; summary adds
-- everything into the IN column and shows a dedicated cache breakdown.

ALTER TABLE telemetry.events
    ADD COLUMN IF NOT EXISTS cache_creation_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS cache_read_tokens     INTEGER;

-- Backfill: extract from existing records (where metadata->>'cache_*' exists).
-- Safe: NULL stays NULL for rows that lacked this info.
UPDATE telemetry.events
   SET cache_creation_tokens = NULLIF(metadata->>'cache_creation_tokens','')::int,
       cache_read_tokens     = NULLIF(metadata->>'cache_read_tokens','')::int
 WHERE cache_creation_tokens IS NULL
   AND cache_read_tokens IS NULL
   AND metadata ? 'cache_creation_tokens';
