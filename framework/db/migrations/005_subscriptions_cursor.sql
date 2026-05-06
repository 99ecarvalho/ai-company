-- 005_subscriptions_cursor.sql
-- Cursor persistido por (user_id, stream_id) pra catch-up de mensagens
-- ao restartar um agente. pg_notify eh fire-and-forget: se o bot caiu
-- entre o INSERT e o _on_notify executar, a msg virava fantasma. Com
-- cursor, o start() busca SELECT id FROM messaging.messages WHERE id > last_read
-- e replaya antes de ativar o LISTEN. Semantica:
--   NULL = nunca leu nada (novo bot) → catch-up pode decidir pular backlog
--          historico OU replayar tudo. Optamos por: NULL == 0 no WHERE id > $,
--          mas o agente no primeiro start seta pro max(id) atual pra nao
--          replayar todo o historico na instalacao.
--   N    = proximo catch-up busca WHERE id > N.
-- Update: feito pelo broker endpoint POST /api/subscriptions/cursor chamado
-- pelo agente apos enfileirar msg no pipeline interno.

ALTER TABLE messaging.subscriptions
    ADD COLUMN IF NOT EXISTS last_read_message_id BIGINT;
