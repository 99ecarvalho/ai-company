#!/bin/bash
# Entrypoint compartilhado por todos os agentes.
# Roda como root, copia credenciais do staging pro volume nomeado deste agente,
# corrige ownership, e dropa para o user node via gosu antes de executar CMD.
set -euo pipefail

STAGING_DIR=/tmp/claude-auth          # file bind (.claude.json)
HOST_CLAUDE_DIR=/tmp/host-claude      # dir bind (~/.claude/, inode-safe)
CLAUDE_HOME=/home/node/.claude
CLAUDE_JSON=/home/node/.claude.json

# Garante que o home do claude existe (volume pode vir vazio em primeira subida)
mkdir -p "$CLAUDE_HOME"

# Seed das credenciais. Daqui pra frente o claude_runner refaz o cp pre-spawn
# pra absorver rotacao de OAuth token sem precisar restartar o container
# (D-90). Se o dir bind nao existir (instancia legada antes da reconcile),
# cai pro file bind antigo do staging.
if [ -f "$HOST_CLAUDE_DIR/.credentials.json" ]; then
  cp -f "$HOST_CLAUDE_DIR/.credentials.json" "$CLAUDE_HOME/.credentials.json"
elif [ -f "$STAGING_DIR/.credentials.json" ]; then
  cp -f "$STAGING_DIR/.credentials.json" "$CLAUDE_HOME/.credentials.json"
fi
if [ -f "$STAGING_DIR/.claude.json" ]; then
  cp -f "$STAGING_DIR/.claude.json" "$CLAUDE_JSON"
fi

# Propaga settings do agente (permissions + hooks D-59) pro nivel user.
# Motivo: Claude Code resolve settings.json via cwd-walker + user-level. Quando
# cwd muda (ex: cwd=worktree do executor), o walker nao encontra mais o
# `/app/agents/<name>/.claude/settings.json`. Copiando pra `~/.claude/settings.json`,
# o CLI sempre enxerga — merge nativo com qualquer project-level.
AGENT_SETTINGS="/app/agents/${AGENT_NAME:-}/.claude/settings.json"
if [ -n "${AGENT_NAME:-}" ] && [ -f "$AGENT_SETTINGS" ]; then
  cp -f "$AGENT_SETTINGS" "$CLAUDE_HOME/settings.json"
fi

# Skills do agente (per-agent library, persistente entre sessoes).
# Claude Code CLI hardcoda write-block em ~/.claude/ mesmo sob bypass —
# entao agente escreve via MCP (save_skill) em /app/agents/<name>/skills/
# (mount RW do host) e symlinkamos ~/.claude/skills -> la pra que o CLI
# carregue no startup. Recriamos o symlink toda subida pra absorver mudanca
# de AGENT_NAME e garantir que conteudo legacy do volume named (raro,
# CLI bloqueia writes la) nao mascare a fonte da verdade no host.
if [ -n "${AGENT_NAME:-}" ]; then
  AGENT_SKILLS_DIR="/app/agents/${AGENT_NAME}/skills"
  mkdir -p "$AGENT_SKILLS_DIR"
  # Chown pra node:node (handler MCP roda como node e precisa escrever).
  # Bind mount propaga ownership pro host — owner do dir vira o uid/gid de
  # node dentro do container (1000:1000), o que esta OK no host (assumindo
  # uid 1000 = usuario do host).
  chown -R node:node "$AGENT_SKILLS_DIR" 2>/dev/null || true
  if [ -e "$CLAUDE_HOME/skills" ] || [ -L "$CLAUDE_HOME/skills" ]; then
    rm -rf "$CLAUDE_HOME/skills"
  fi
  ln -s "$AGENT_SKILLS_DIR" "$CLAUDE_HOME/skills"
fi

# Fix de ownership (volume novo pode vir com uid/gid 0)
chown -R node:node "$CLAUDE_HOME" 2>/dev/null || true
# Workspace dirs que o agente precisa escrever (worktrees, repos).
# Bind mounts do host podem chegar com uid 0 (bootstrap como root).
chown -R node:node /workspace/worktrees 2>/dev/null || true
chown -R node:node /workspace/repos 2>/dev/null || true
if [ -f "$CLAUDE_JSON" ]; then
  chown node:node "$CLAUDE_JSON" 2>/dev/null || true
fi

# Dropa para user node e executa CMD
exec gosu node "$@"
