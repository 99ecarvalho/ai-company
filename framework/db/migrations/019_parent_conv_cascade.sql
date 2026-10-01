-- 019: FK parent_conv_id changes from ON DELETE SET NULL -> ON DELETE CASCADE
--
-- Context: migration 017 (D-87) introduced `parent_conv_id` with ON DELETE
-- SET NULL. The intent was conservative: if something deleted a parent, children
-- would survive. In practice this creates visible orphans — the child shows up in the
-- sidebar with no hierarchy anchor, no way back to the context of the
-- original ask_agent.
--
-- The delete/archive endpoints in main.py already do a logical cascade via
-- _collect_descendant_conv_ids(). Switching the FK to CASCADE, the DB
-- guarantees consistency even in edge cases (failure in the collect function,
-- direct DELETE via psql, new descendant created between collect and DELETE).
--
-- Archive (soft delete) keeps working via UPDATE + recursive function
-- — CASCADE only acts on a physical DELETE.
--
-- The migration reuses the constraint name to stay compatible.

ALTER TABLE messaging.conversations
    DROP CONSTRAINT conversations_parent_conv_id_fkey;

ALTER TABLE messaging.conversations
    ADD CONSTRAINT conversations_parent_conv_id_fkey
    FOREIGN KEY (parent_conv_id)
    REFERENCES messaging.conversations(id)
    ON DELETE CASCADE;
