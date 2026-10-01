#!/bin/bash
# Shortcut to create a new agent: appends a minimal entry to
# instance/agents/agents.yaml and triggers reconcile. For advanced config, edit
# the yaml by hand afterwards and run `framework/scripts/reconcile.sh`.
#
# Usage:
#   framework/scripts/new-agent.sh <name> "<Display Name>"
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$PROJECT_ROOT"

if [ $# -lt 2 ]; then
  echo "Usage: $0 <name> \"<Display Name>\""
  echo "  name: lowercase slug (e.g. researcher, writer)"
  echo "  Display Name: how it appears in the UI (e.g. \"Researcher\", \"Writer\")"
  exit 1
fi

NAME="$1"
DISPLAY_NAME="$2"
AGENTS_YAML="instance/agents/agents.yaml"

# Validate the name
if ! [[ "$NAME" =~ ^[a-z][a-z0-9-]{0,30}$ ]]; then
  echo "ERROR: name must be lowercase, start with a letter, only [a-z0-9-]. Got: $NAME" >&2
  exit 1
fi

# Check whether it already exists
if grep -qE "^  - name: $NAME\$" "$AGENTS_YAML" 2>/dev/null; then
  echo "ERROR: agent '$NAME' already exists in $AGENTS_YAML" >&2
  exit 1
fi

if [ ! -f "$AGENTS_YAML" ]; then
  echo "ERROR: $AGENTS_YAML does not exist. Use framework/examples/agents.yaml.example as a base." >&2
  exit 1
fi

# Append a minimal entry (essentials only; the user adjusts it later)
cat >> "$AGENTS_YAML" <<EOF

  - name: $NAME
    display_name: "$DISPLAY_NAME"
    description: "TODO: describe this agent's role in one line."
    streams: [$NAME]
    pool_size: 2
    idle_timeout_sec: 900
    write_access: [company]
    read_access: []
    allowed_tools:                    # on top of allowed_tools_defaults, if any
      - group:files
      - group:human
      - group:workflow
      - group:memory
EOF

echo "✓ entry '$NAME' added to $AGENTS_YAML"
echo ""
echo "Edit the fields (description, write_access, allowed_tools, etc) before applying."
echo "When ready, run:"
echo "  ./framework/scripts/reconcile.sh"
echo ""
echo "To apply right now (with the defaults), answer 'y':"
read -r -p "Apply now? (y/N) " APPLY
if [ "$APPLY" = "y" ] || [ "$APPLY" = "Y" ]; then
  ./framework/scripts/reconcile.sh
fi
