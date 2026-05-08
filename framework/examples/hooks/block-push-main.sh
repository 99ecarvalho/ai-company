#!/bin/bash
# block-push-main.sh — Claude Code PreToolUse hook.
#
# Blocks attempts by the agent to `git push` against main/master. Agents
# must push to a branch (e.g. task/<slug>) and open an MR/PR.
#
# Reference in agents.yaml via hooks_defaults:
#   hooks_defaults:
#     PreToolUse:
#       - matcher: "Bash"
#         hooks:
#           - type: command
#             command: "/app/hooks/block-push-main.sh"
#
# Requirements:
# - Installed at /app/hooks/ via bind mount (D-60). Uses `jq` (present in
#   the agent image).
# - There is NO env-var escape hatch (the agent would control it) — if you
#   need a direct push to main, the human runs it from the host (zero impact
#   on the host) or temporarily edits hooks_defaults + reconcile + restart.
#
# Exit codes:
#   exit 0 → allow
#   exit 2 → block, stderr becomes feedback for Claude
#
# Heuristic: parses tool_input.command, looks for "git push" + main/master target.
# Covers: `git push`, `git push origin main`, `git push origin HEAD:main`,
#         `cd foo && git push`, compound with `;`/`&&`/`||`.
# Does not cover: push via script written by the agent, custom alias, subprocess
#            outside Bash MCP. These cases are rare in the standard workflow.

set -euo pipefail

INPUT=$(cat)
CMD=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

if [[ -z "$CMD" ]]; then
  exit 0
fi

# Split the command into simple subcommands to analyze each one. Shell-level
# separators that start a new command: ; && || | &  (& backgrounded too).
# tr replaces them with newlines, then we read line by line.
SUBS=$(echo "$CMD" | tr ';|&' '\n')

while IFS= read -r sub; do
  # Trim + skip empty
  sub="$(echo "$sub" | sed -E 's/^[[:space:]]+|[[:space:]]+$//g')"
  [[ -z "$sub" ]] && continue

  # Look for "git push" — allow flags+values between git and push
  # (e.g. `git -C /tmp push`, `git --no-pager push`, `git -c key=val push`).
  # Any token without a shell separator (;|&) can appear.
  if ! echo "$sub" | grep -qE '(^|[[:space:]])git([[:space:]]+[^[:space:]]+)*[[:space:]]+push([[:space:]]|$)'; then
    continue
  fi

  # Found a git push. Now check whether the target is main/master. Patterns:
  #   git push                              → default branch (could be main) — block as a precaution
  #   git push origin                       → default — block
  #   git push origin main                  → block
  #   git push origin master                → block
  #   git push origin HEAD:main             → block
  #   git push origin HEAD:refs/heads/main  → block
  #   git push origin task/foo              → allow
  #   git push origin HEAD:task/foo         → allow
  #   git push --delete origin main         → allow (branch delete is not a merge)

  # Delete: allow.
  if echo "$sub" | grep -qE '(^|[[:space:]])(--delete|-d)([[:space:]]|$)'; then
    continue
  fi

  # Extract args after "push". Use sed to grab everything after the first "push".
  ARGS=$(echo "$sub" | sed -E 's/^.*[[:space:]]push([[:space:]]|$)//')

  # If there are no explicit args, git uses upstream/default — block as a safeguard.
  if [[ -z "$ARGS" ]] || echo "$ARGS" | grep -qE '^-[^[:space:]]*([[:space:]]|$)'; then
    # No visible refspec OR only flags. Can't be sure the target isn't main.
    if [[ -z "$ARGS" ]]; then
      echo "✗ block-push-main: 'git push' without an explicit refspec — could go to main via upstream." >&2
      echo "  Use: git push origin HEAD:task/<slug>" >&2
      exit 2
    fi
  fi

  # Look for main/master tokens as refspec. Matching patterns:
  #   main | master | <src>:main | <src>:master | <src>:refs/heads/main | refs/heads/main
  if echo "$ARGS" | grep -qE '(^|[[:space:]:])((refs/heads/)?(main|master))([[:space:]]|$)'; then
    echo "" >&2
    echo "✗ block-push-main: direct push to main/master blocked." >&2
    echo "  Create a branch (e.g. task/<slug>), push it, and open an MR/PR:" >&2
    echo "    git push origin HEAD:task/<slug>" >&2
    echo "    glab mr create --target-branch main   # or: gh pr create --base main" >&2
    echo "" >&2
    exit 2
  fi
done <<< "$SUBS"

exit 0
