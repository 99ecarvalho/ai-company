-- 031: web.app_settings — generic KV for instance config editable
-- via PWA Settings instead of .env. Replaces (with fallback) the env vars
-- WEB_DEFAULT_STREAM, VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_CONTACT_EMAIL.
--
-- Motivation: fresh instances ended up with push disabled and
-- default_stream pointing to a nonexistent agent, with no visibility in the
-- PWA. Moving it to the DB solves two things:
--   1. Editable at runtime without touching files + restart
--   2. Onboarding can auto-detect gaps + offer a "Generate VAPID" button
--
-- Fallback policy: callsites read from the DB first; if missing, they fall back to the
-- equivalent env var. Existing instances do not break. Eventually the
-- env vars get removed from bootstrap-env.sh (new instances).
--
-- Schema: deliberately generic KV (1 row per key, value JSONB) instead
-- of a column per setting. Adding a new setting = upsert, no migration.

CREATE TABLE web.app_settings (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  INTEGER REFERENCES messaging.users(id) ON DELETE SET NULL
);

COMMENT ON TABLE web.app_settings IS
    'Instance config editable from PWA Settings. Current canonical keys: '
    '"default_stream" (string), "vapid" ({public_key, private_key, contact_email}). '
    'Callers fall back to the equivalent env vars when a key is missing.';
