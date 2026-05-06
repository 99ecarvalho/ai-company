-- 011: tombstones de topics deletados pra evitar ressurgimento pos-DELETE.
--
-- Problema (D-72): quando o humano deleta uma conversation via PWA, o backend
-- faz DELETE na `messaging.conversations` e dispara cancel via pg_notify
-- pra liberar runners ativos. O dispatcher responde ao cancel postando uma
-- msg de confirmacao ("Cancelado pelo usuario" ou "Nada pra cancelar"). Essa
-- msg passa por `_get_or_create_conversation` que AUTO-CRIA a conv se nao
-- existe — resultado: a conv ressurge com a mensagem do dispatcher, anulando
-- o delete do humano.
--
-- Fix: registrar um tombstone `(stream_id, topic_name, deleted_at)` antes do
-- DELETE. `_get_or_create_conversation` consulta o tombstone: se deletado ha
-- menos de `TOMBSTONE_TTL_SEC` (default 5min, env), raise 410 Gone — dispatcher
-- loga warn, descarta a msg, e o slot de pool fica liberado sem ressurgir
-- a conv.
--
-- TTL curto (5min) porque topics `__ask-from-<agent>-<uid>` tem uid random e
-- nao colidem; tombstone expirado eh inofensivo. Cleanup periodico via
-- scheduler job (futuro) ou simples DELETE WHERE deleted_at < now() - interval.
--
-- Aditivo: tabela nova, zero impacto em schema existente. Reversivel trivial.

CREATE TABLE messaging.deleted_topics (
    stream_id  INTEGER NOT NULL REFERENCES messaging.streams(id) ON DELETE CASCADE,
    topic_name TEXT NOT NULL,
    deleted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (stream_id, topic_name)
);

-- Index pra consulta rapida por tempo (cleanup + TTL check).
CREATE INDEX idx_deleted_topics_deleted_at
    ON messaging.deleted_topics (deleted_at);
