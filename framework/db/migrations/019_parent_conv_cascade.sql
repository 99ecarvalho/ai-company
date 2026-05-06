-- 019: FK parent_conv_id passa de ON DELETE SET NULL -> ON DELETE CASCADE
--
-- Contexto: migration 017 (D-87) introduziu `parent_conv_id` com ON DELETE
-- SET NULL. A intencao era conservadora: se algo deletasse um pai, filhos
-- sobreviveriam. Na pratica isso cria orfaos visiveis — filho aparece na
-- sidebar sem ancora de hierarquia, sem caminho de volta ao contexto do
-- ask_agent original.
--
-- Endpoints de delete/archive em main.py ja fazem cascade logica via
-- _collect_descendant_conv_ids(). Mudando o FK pra CASCADE, o banco
-- garante consistencia mesmo em edge cases (falha na funcao de coleta,
-- DELETE direto via psql, descendente novo criado entre coleta e DELETE).
--
-- Archive (soft delete) continua funcionando via UPDATE + funcao recursiva
-- — CASCADE so atua em DELETE fisico.
--
-- Migration reusa o nome do constraint pra manter compat.

ALTER TABLE messaging.conversations
    DROP CONSTRAINT conversations_parent_conv_id_fkey;

ALTER TABLE messaging.conversations
    ADD CONSTRAINT conversations_parent_conv_id_fkey
    FOREIGN KEY (parent_conv_id)
    REFERENCES messaging.conversations(id)
    ON DELETE CASCADE;
