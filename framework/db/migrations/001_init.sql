-- init.sql — single ai-company schema (Postgres unifies messaging +
-- orchestrator + memory + telemetry + web).
--
-- Applied automatically by the postgres container on first boot (via
-- /docker-entrypoint-initdb.d/). Data persists in the postgres-data volume.
--
-- To apply schema changes after the initial boot: drop & recreate the DB,
-- or write ALTER TABLE in a separate migration file (TODO: alembic if it gets
-- complex).
--
-- Schemas by domain:
--   messaging     — chat broker (streams, topics, messages, asks)
--   orchestrator  — workflow events between agents
--   memory        — persistent facts per agent
--   telemetry     — Claude run stats (cost, tokens, latency)
--   web           — PWA state (push subs, closed conversations)

CREATE SCHEMA IF NOT EXISTS messaging;
CREATE SCHEMA IF NOT EXISTS orchestrator;
CREATE SCHEMA IF NOT EXISTS memory;
CREATE SCHEMA IF NOT EXISTS telemetry;
CREATE SCHEMA IF NOT EXISTS web;


-- ============================================================
-- messaging: chat broker
-- ============================================================

CREATE TABLE messaging.users (
    id            SERIAL PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    username      TEXT UNIQUE NOT NULL,
    full_name     TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('human', 'bot')),
    agent_name    TEXT,                 -- set when kind='bot'; agent name
    api_token     TEXT UNIQUE,          -- Bearer token for bots (generated at reconcile)
    password_hash TEXT,                 -- bcrypt for humans (future)
    is_admin      BOOLEAN NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE messaging.streams (
    id            SERIAL PRIMARY KEY,
    name          TEXT UNIQUE NOT NULL, -- e.g. 'assistant', 'debug', 'orchestration'
    description   TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE messaging.subscriptions (
    user_id       INTEGER NOT NULL REFERENCES messaging.users(id) ON DELETE CASCADE,
    stream_id     INTEGER NOT NULL REFERENCES messaging.streams(id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, stream_id)
);

CREATE TABLE messaging.conversations (
    id              SERIAL PRIMARY KEY,
    stream_id       INTEGER NOT NULL REFERENCES messaging.streams(id) ON DELETE CASCADE,
    topic_name      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_message_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (stream_id, topic_name)
);
CREATE INDEX conversations_stream_last ON messaging.conversations (stream_id, last_message_at DESC);

CREATE TABLE messaging.messages (
    id               BIGSERIAL PRIMARY KEY,
    conversation_id  INTEGER NOT NULL REFERENCES messaging.conversations(id) ON DELETE CASCADE,
    sender_id        INTEGER NOT NULL REFERENCES messaging.users(id),
    content          TEXT NOT NULL,
    client_id        TEXT,              -- idempotency (caller-supplied)
    sent_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (conversation_id, client_id)
);
CREATE INDEX messages_conv_time ON messaging.messages (conversation_id, sent_at);
CREATE INDEX messages_sender_time ON messaging.messages (sender_id, sent_at);
-- Global FTS search (used by GET /api/search). simple = no language-specific stemming.
CREATE INDEX messages_fts_idx ON messaging.messages USING gin (to_tsvector('simple', content));

-- agent_policies: rules for who can talk to whom via ask_agent.
-- Editable via /api/agent-policies from the UI (checkbox matrix). NULL in both
-- columns = no restriction. Validated in claude_runner._on_ask_agent.
CREATE TABLE messaging.agent_policies (
    agent              TEXT PRIMARY KEY,
    can_ask            TEXT[],
    can_be_asked_by    TEXT[],
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- pending_asks: at most 1 blocking question per conversation (broker enforces).
-- Reused by ask_human (human answers) and ask_agent (target agent answers).
CREATE TABLE messaging.pending_asks (
    conversation_id   INTEGER PRIMARY KEY REFERENCES messaging.conversations(id) ON DELETE CASCADE,
    asker_id          INTEGER NOT NULL REFERENCES messaging.users(id),
    question          TEXT NOT NULL,
    context           TEXT,
    blocking          BOOLEAN NOT NULL DEFAULT true,
    asked_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at       TIMESTAMPTZ,
    answer_message_id BIGINT REFERENCES messaging.messages(id)
);


-- ============================================================
-- orchestrator: workflow events (formerly: instance/events/*.json)
-- ============================================================

CREATE TABLE orchestrator.events (
    id            BIGSERIAL PRIMARY KEY,
    emitted_by    TEXT NOT NULL,          -- agent name
    event_type    TEXT NOT NULL,          -- e.g. 'phase_complete'
    task_slug     TEXT,
    payload       JSONB NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','processing','processed','quarantined','dead')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at  TIMESTAMPTZ
);
CREATE INDEX events_status_time ON orchestrator.events (status, created_at);


-- ============================================================
-- memory: persistent facts per agent (formerly: SQLite /memory/*.db)
-- ============================================================

CREATE TABLE memory.facts (
    id           BIGSERIAL PRIMARY KEY,
    agent        TEXT NOT NULL,
    key          TEXT NOT NULL,
    value        TEXT NOT NULL,
    tags         TEXT[] NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (agent, key)
);
CREATE INDEX facts_agent_updated ON memory.facts (agent, updated_at DESC);
CREATE INDEX facts_tags ON memory.facts USING gin (tags);
CREATE INDEX facts_fts ON memory.facts USING gin (to_tsvector('simple', key || ' ' || value));


-- ============================================================
-- telemetry: Claude run stats
-- ============================================================

CREATE TABLE telemetry.events (
    id             BIGSERIAL PRIMARY KEY,
    agent          TEXT NOT NULL,
    topic_slug     TEXT,
    event_type     TEXT NOT NULL,          -- claude_run_end, claude_run_error, etc
    cost_usd       NUMERIC(10, 6),
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    duration_ms    INTEGER,
    model          TEXT,
    metadata       JSONB,
    ts             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX telemetry_agent_time ON telemetry.events (agent, ts DESC);
CREATE INDEX telemetry_time ON telemetry.events (ts DESC);

-- Live trace events: captures each event of claude's stream-json (run_start,
-- thinking, tool_use, tool_result, run_end) per conversation. Consumed via
-- SSE to show real-time activity in the PWA. Cleanup via scheduler.
CREATE TABLE telemetry.live_events (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES messaging.conversations(id) ON DELETE CASCADE,
    agent           TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind            TEXT NOT NULL,      -- run_start | thinking | tool_use | tool_result | run_end
    summary         TEXT,
    data            JSONB
);
CREATE INDEX idx_live_events_conv_ts ON telemetry.live_events (conversation_id, ts DESC);

-- pg_notify trigger: emits on channel `live_event_<conv_id>` on each INSERT.
CREATE OR REPLACE FUNCTION telemetry.notify_live_event() RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify(
        'live_event_' || NEW.conversation_id,
        json_build_object(
            'id', NEW.id,
            'agent', NEW.agent,
            'ts', extract(epoch from NEW.ts),
            'kind', NEW.kind,
            'summary', NEW.summary
        )::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_notify_live_event AFTER INSERT ON telemetry.live_events
FOR EACH ROW EXECUTE FUNCTION telemetry.notify_live_event();


-- ============================================================
-- web: PWA state
-- ============================================================

CREATE TABLE web.push_subscriptions (
    id          BIGSERIAL PRIMARY KEY,
    endpoint    TEXT UNIQUE NOT NULL,
    p256dh      TEXT NOT NULL,
    auth        TEXT NOT NULL,
    user_id     INTEGER REFERENCES messaging.users(id) ON DELETE CASCADE,
    user_agent  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE web.closed_conversations (
    conversation_id  INTEGER NOT NULL REFERENCES messaging.conversations(id) ON DELETE CASCADE,
    user_id          INTEGER NOT NULL REFERENCES messaging.users(id) ON DELETE CASCADE,
    closed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (conversation_id, user_id)
);

-- Cost budgets per agent (in USD/day). Scheduler action `cost_budget_check`
-- compares aggregated spend from telemetry.events with daily_usd_limit. When
-- exceeded, posts an alert on the agent's stream and records it in budget_alerts
-- to avoid spamming (1-day cooldown).
CREATE TABLE web.cost_budgets (
    agent             TEXT PRIMARY KEY,
    daily_usd_limit   NUMERIC(10, 4) NOT NULL CHECK (daily_usd_limit > 0),
    alert_message     TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE web.budget_alerts (
    agent       TEXT NOT NULL,
    alert_date  DATE NOT NULL,
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    spent_usd   NUMERIC(10, 4) NOT NULL,
    PRIMARY KEY (agent, alert_date)
);

-- Human sessions in the PWA. Created at /api/auth/login (bcrypt check),
-- consumed via HttpOnly cookie in get_principal. Explicit expiry per
-- column; cleanup of expired ones is on-the-fly (no cron needed).
CREATE TABLE web.sessions (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES messaging.users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    user_agent  TEXT
);
CREATE INDEX idx_web_sessions_expires ON web.sessions(expires_at);


-- ============================================================
-- pg_notify conventions:
--   Channel 'msg_conv_<conversation_id>' receives INSERTs on messaging.messages
--     (topic-scoped; used by ask_agent to avoid over-subscribing — see 003)
--   Channel 'msg_stream_<stream_id>' receives the same INSERTs (stream-scoped)
--   Channel 'msg_all' receives all INSERTs (used by web: SSE + push)
--   Channel 'ask_<conversation_id>' notifies resolution of a pending_ask
--   Channel 'event_new' notifies a new entry in orchestrator.events
-- Triggers are defined here; broker/reactor do the LISTEN.
-- ============================================================

CREATE OR REPLACE FUNCTION messaging.notify_message() RETURNS TRIGGER AS $$
DECLARE
    stream_id INTEGER;
    payload JSONB;
BEGIN
    SELECT c.stream_id INTO stream_id FROM messaging.conversations c WHERE c.id = NEW.conversation_id;
    payload = jsonb_build_object(
        'id', NEW.id,
        'conversation_id', NEW.conversation_id,
        'sender_id', NEW.sender_id,
        'content', NEW.content,
        'sent_at', NEW.sent_at
    );
    PERFORM pg_notify('msg_conv_' || NEW.conversation_id, payload::text);
    PERFORM pg_notify('msg_stream_' || stream_id, payload::text);
    PERFORM pg_notify('msg_all', payload::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_notify_message
    AFTER INSERT ON messaging.messages
    FOR EACH ROW
    EXECUTE FUNCTION messaging.notify_message();


CREATE OR REPLACE FUNCTION messaging.notify_ask_resolved() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.resolved_at IS NOT NULL AND (OLD.resolved_at IS NULL) THEN
        PERFORM pg_notify('ask_' || NEW.conversation_id, jsonb_build_object(
            'conversation_id', NEW.conversation_id,
            'answer_message_id', NEW.answer_message_id,
            'resolved_at', NEW.resolved_at
        )::text);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_notify_ask_resolved
    AFTER UPDATE ON messaging.pending_asks
    FOR EACH ROW
    EXECUTE FUNCTION messaging.notify_ask_resolved();


CREATE OR REPLACE FUNCTION orchestrator.notify_event() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status = 'pending' THEN
        PERFORM pg_notify('event_new', jsonb_build_object('id', NEW.id, 'event_type', NEW.event_type)::text);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_notify_event
    AFTER INSERT ON orchestrator.events
    FOR EACH ROW
    EXECUTE FUNCTION orchestrator.notify_event();


-- ============================================================
-- Base streams created at bootstrap (via script, not here)
-- (other streams are created via broker/reconcile)
-- ============================================================
