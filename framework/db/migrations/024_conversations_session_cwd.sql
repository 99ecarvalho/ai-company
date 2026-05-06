-- 024: claude_session_cwd em messaging.conversations.
--
-- Claude CLI armazena `<session_id>.jsonl` em ~/.claude/projects/<encoded-cwd>/.
-- O directory project e per-cwd: o mesmo session_id so e legivel pelo
-- `claude --resume` quando o spawn_cwd casa com o cwd que o gerou.
--
-- Antes desta coluna, o runner persistia apenas `claude_session_id`. Quando
-- `_resolve_spawn_cwd` retornava cwd diferente entre runs do mesmo topic
-- (ex: agente assumiu uma task em paralelo e ganhou worktree, depois liberou),
-- o `--resume` ia pra um project dir que nao tinha o jsonl daquele id —
-- ghost session, recovery zerava o contexto. Persistir o cwd permite que o
-- runner pule o `--resume` proativamente quando o cwd atual difere do
-- registrado, mantendo o sid pra runs futuras que voltarem ao mesmo cwd.
--
-- Aditivo: NULL default (legacy: sids gravados pre-migration nao tem cwd
-- registrado, e o runner trata como "sem cwd vinculado, aceita qualquer").
-- Reversivel via DROP COLUMN claude_session_cwd.

ALTER TABLE messaging.conversations
  ADD COLUMN claude_session_cwd TEXT;
