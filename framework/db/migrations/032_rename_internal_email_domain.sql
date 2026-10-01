-- 032: the project was renamed from agent-framework to ai-company.
--
-- Bot and system users are upserted by email, and the email domain moved
-- from @internal.agent-framework to @internal.ai-company. Without this,
-- reconcile would try to insert a second user with the same username and
-- hit the unique constraint. Idempotent: rows already on the new domain
-- are left alone.
UPDATE messaging.users
   SET email = replace(email, '@internal.agent-framework', '@internal.ai-company')
 WHERE email LIKE '%@internal.agent-framework';
