-- 010: reverte a soma que 004 fez em telemetry.events.input_tokens
--
-- Contexto: migration 004 e claude_runner passaram a armazenar
-- `input_tokens = base + cache_creation + cache_read` pra "IN" do PWA
-- refletir o total cobrado. Na pratica isso criou duas distorcoes:
--   1. Nome colide com a semantica da API da Anthropic, onde `input_tokens`
--      significa APENAS tokens nao-cacheados (os outros dois contadores sao
--      disjuntos e tem preco proprio).
--   2. Como cache domina >99% na maioria dos runs, a coluna IN e a coluna
--      cache aparecem identicas no PWA, escondendo quanto e realmente input
--      novo.
--
-- Daqui pra frente claude_runner grava so o base (nao-cacheado) em
-- input_tokens, e o PWA pode mostrar os 3 contadores separados quando quiser
-- soma. Este backfill restaura os registros existentes ao mesmo formato.
--
-- Seguro: GREATEST(..., 0) cobre eventuais linhas onde a aritmetica nao
-- fecharia exato (raro, por arredondamento do CLI). NULL cache permanece
-- NULL e input_tokens fica intocado.

UPDATE telemetry.events
   SET input_tokens = GREATEST(
         input_tokens
           - COALESCE(cache_creation_tokens, 0)
           - COALESCE(cache_read_tokens, 0),
         0)
 WHERE input_tokens IS NOT NULL
   AND (cache_creation_tokens IS NOT NULL OR cache_read_tokens IS NOT NULL);
