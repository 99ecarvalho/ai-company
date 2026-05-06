-- 028_messaging_is_active.sql
-- Soft-delete pra users e streams que correspondem a agentes desativados.
--
-- Problema: quando um agente sai de instance/agents/agents.yaml, o reconcile
-- atual nao remove a row em messaging.users / messaging.streams. Ficam
-- artefatos (`tech-lead-bot`, stream `tech-lead`) que continuam aparecendo
-- em `_team_block` (claude_runner monta '## Equipe' lendo todos bots) e em
-- listagens da PWA. PO entao recebia "tech-lead" no system prompt como
-- peer valido e chamava `ask_agent(target_agent='tech-lead')` — a conv
-- filha era criada mas ficava parada porque nao ha container.
--
-- Decisao: adicionar `is_active boolean DEFAULT true` em ambas tabelas
-- (soft-delete preserva historico de convs antigas com refs a esses
-- streams/users). Reconcile passa a marcar `is_active=false` quando
-- agente sai do yaml. Filtros de UI/prompt usam `is_active=true`.

ALTER TABLE messaging.users
    ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT true;

ALTER TABLE messaging.streams
    ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT true;

COMMENT ON COLUMN messaging.users.is_active IS
    'false = agente removido do agents.yaml (soft-delete). Filtra de '
    'system prompts (## Equipe) e listagens da PWA. Historico preservado.';

COMMENT ON COLUMN messaging.streams.is_active IS
    'false = stream do agente removido do agents.yaml (soft-delete). '
    'Convs antigas continuam acessiveis mas stream nao aparece em UIs.';

CREATE INDEX IF NOT EXISTS users_active_bot_idx
    ON messaging.users (agent_name)
 WHERE kind = 'bot' AND is_active = true AND agent_name IS NOT NULL;

CREATE INDEX IF NOT EXISTS streams_active_idx
    ON messaging.streams (name)
 WHERE is_active = true;
