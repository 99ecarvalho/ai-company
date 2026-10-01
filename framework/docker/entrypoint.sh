#!/bin/bash
# Entrypoint shared by all agents.
# Runs as root, copies credentials from staging into this agent's named volume,
# fixes ownership, and drops to the node user via gosu before running CMD.
set -euo pipefail

STAGING_DIR=/tmp/claude-auth          # file bind (.claude.json)
HOST_CLAUDE_DIR=/tmp/host-claude      # dir bind (~/.claude/, inode-safe)
CLAUDE_HOME=/home/node/.claude
CLAUDE_JSON=/home/node/.claude.json

# Make sure claude's home exists (the volume may be empty on first start)
mkdir -p "$CLAUDE_HOME"

# Seed the credentials. From here on claude_runner repeats the cp before each
# spawn to pick up OAuth token rotation without restarting the container
# (D-90). If the dir bind does not exist (legacy instance before reconcile),
# fall back to the old staging file bind.
if [ -f "$HOST_CLAUDE_DIR/.credentials.json" ]; then
  cp -f "$HOST_CLAUDE_DIR/.credentials.json" "$CLAUDE_HOME/.credentials.json"
elif [ -f "$STAGING_DIR/.credentials.json" ]; then
  cp -f "$STAGING_DIR/.credentials.json" "$CLAUDE_HOME/.credentials.json"
fi
if [ -f "$STAGING_DIR/.claude.json" ]; then
  cp -f "$STAGING_DIR/.claude.json" "$CLAUDE_JSON"
fi

# Propagate the agent's settings (permissions + hooks D-59) to user level.
# Reason: Claude Code resolves settings.json via a cwd walker + user level. When
# the cwd changes (e.g. cwd=an agent's worktree), the walker no longer finds
# `/app/agents/<name>/.claude/settings.json`. Copied to `~/.claude/settings.json`,
# the CLI always sees it — native merge with any project-level settings.
AGENT_SETTINGS="/app/agents/${AGENT_NAME:-}/.claude/settings.json"
if [ -n "${AGENT_NAME:-}" ] && [ -f "$AGENT_SETTINGS" ]; then
  cp -f "$AGENT_SETTINGS" "$CLAUDE_HOME/settings.json"
fi

# Agent skills (per-agent library, persistent across sessions).
# The Claude Code CLI hardcodes a write block on ~/.claude/ even under bypass —
# so the agent writes via MCP (save_skill) to /app/agents/<name>/skills/
# (RW host mount) and we symlink ~/.claude/skills -> there so the CLI loads
# them at startup. The symlink is recreated on every start to pick up a changed
# AGENT_NAME and to make sure legacy content in the named volume (rare, the
# CLI blocks writes there) does not mask the source of truth on the host.
if [ -n "${AGENT_NAME:-}" ]; then
  AGENT_SKILLS_DIR="/app/agents/${AGENT_NAME}/skills"
  mkdir -p "$AGENT_SKILLS_DIR"
  # Chown to node:node (the MCP handler runs as node and needs to write).
  # The bind mount propagates ownership to the host — the dir owner becomes
  # node's uid/gid inside the container (1000:1000), which is fine on the host
  # (assuming uid 1000 = the host user).
  chown -R node:node "$AGENT_SKILLS_DIR" 2>/dev/null || true
  if [ -e "$CLAUDE_HOME/skills" ] || [ -L "$CLAUDE_HOME/skills" ]; then
    rm -rf "$CLAUDE_HOME/skills"
  fi
  ln -s "$AGENT_SKILLS_DIR" "$CLAUDE_HOME/skills"
fi

# Fix ownership (a new volume may come with uid/gid 0)
chown -R node:node "$CLAUDE_HOME" 2>/dev/null || true
# Writable workspace dirs (worktrees, git dirs of init_repo repos). Host bind
# mounts can arrive owned by uid 0 (bootstrap run as root). /workspace/repos
# itself is read-only (D-115), so only its .gitdirs mount is touched.
chown -R node:node /workspace/worktrees 2>/dev/null || true
chown -R node:node /workspace/repos/.gitdirs 2>/dev/null || true
if [ -f "$CLAUDE_JSON" ]; then
  chown node:node "$CLAUDE_JSON" 2>/dev/null || true
fi

# Drop to the node user and run CMD
exec gosu node "$@"
