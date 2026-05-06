-- 027_conversations_custom_title.sql
-- Titulo customizavel por conversa, independente do topic_name (que e o ID
-- estavel da conversa no broker e geralmente reflete um pattern operacional:
-- 'task-<slug>', '__ask-from-X-Y', '__child-...', etc).
--
-- Problema: o humano olha a sidebar pra entender quais conversas estao
-- ativas, mas o topic_name muitas vezes e tecnico/repetitivo (varios
-- 'task-fix-...' lado a lado). O `task.title` ja ajuda quando ha task
-- atrelada, mas pra conversas livres ou pra dar um apelido contextual,
-- o humano precisa de um campo proprio.
--
-- Decisao: coluna `custom_title` nullable em messaging.conversations.
-- Display rule no frontend: `custom_title || task.title || topic`. Editavel
-- inline na sidebar. NULL = sem override (cai no fallback). Backfill inicial
-- copia topic_name pra todas as conversas existentes — assim quando o
-- humano clicar pra editar pela primeira vez ja ve o valor atual no input
-- (sem aparecer vazio). Conversas novas nascem com NULL e a regra de
-- fallback cobre.

ALTER TABLE messaging.conversations
    ADD COLUMN custom_title TEXT;

COMMENT ON COLUMN messaging.conversations.custom_title IS
    'Titulo customizavel pelo humano via PWA. NULL = usa fallback '
    '(task.title || topic_name). Editavel inline na sidebar.';

UPDATE messaging.conversations
   SET custom_title = topic_name
 WHERE custom_title IS NULL;
