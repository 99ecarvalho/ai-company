-- 004_telemetry_cache_tokens.sql
-- Adiciona colunas dedicadas pra cache tokens em telemetry.events e backfill
-- a partir do JSONB metadata (onde claude_runner hoje enfia cache_*).
-- Contexto: a coluna `input_tokens` histórica carrega só tokens NAO-cacheados
-- (o que o Claude CLI chama de `input_tokens` no usage). Cache creation/read
-- ficavam só em `metadata.cache_creation_tokens` / `metadata.cache_read_tokens`
-- e nunca entravam no summary — PWA mostrava IN fake (muito menor que real).
-- Daqui pra frente claude_runner envia os 3 campos separados; summary soma
-- tudo pra coluna IN e exibe cache breakdown dedicado.

ALTER TABLE telemetry.events
    ADD COLUMN IF NOT EXISTS cache_creation_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS cache_read_tokens     INTEGER;

-- Backfill: extrai dos registros existentes (onde metadata->>'cache_*' existe).
-- Seguro: NULL stays NULL pra linhas que nao tinham essa info.
UPDATE telemetry.events
   SET cache_creation_tokens = NULLIF(metadata->>'cache_creation_tokens','')::int,
       cache_read_tokens     = NULLIF(metadata->>'cache_read_tokens','')::int
 WHERE cache_creation_tokens IS NULL
   AND cache_read_tokens IS NULL
   AND metadata ? 'cache_creation_tokens';
