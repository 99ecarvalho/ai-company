-- 031: web.app_settings — KV generico pra config de instancia editavel
-- via PWA Settings em vez de .env. Substitui (com fallback) os env vars
-- WEB_DEFAULT_STREAM, VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_CONTACT_EMAIL.
--
-- Motivacao: instancias fresh ficavam com push desabilitado e
-- default_stream apontando pra agente inexistente, sem visibilidade no
-- PWA. Mover pra DB resolve dois pontos:
--   1. Editavel em runtime sem mexer em arquivo + restart
--   2. Onboarding pode auto-detectar gaps + oferecer "Generate VAPID" button
--
-- Politica de fallback: callsites leem da DB primeiro; se ausente, caem no
-- env var equivalente. Instancias existentes nao quebram. Eventualmente
-- env vars sao removidos do bootstrap-env.sh (novas instancias).
--
-- Schema: KV deliberadamente generico (1 row por chave, value JSONB) em
-- vez de coluna-por-setting. Adicionar setting novo = upsert, sem migration.

CREATE TABLE web.app_settings (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  INTEGER REFERENCES messaging.users(id) ON DELETE SET NULL
);

COMMENT ON TABLE web.app_settings IS
    'Config de instancia editavel via PWA Settings. Chaves canonicas hoje: '
    '"default_stream" (string), "vapid" ({public_key, private_key, contact_email}). '
    'Callsites caem em env vars equivalentes quando a chave nao existe.';
