#!/bin/bash
# Atalho pra criar um novo agente: acrescenta entry minima em
# instance/agents/agents.yaml e dispara o reconcile. Pra config avancada, edite
# o yaml manualmente depois e rode `framework/scripts/reconcile.sh`.
#
# Uso:
#   framework/scripts/new-agent.sh <name> "<Display Name>"
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$PROJECT_ROOT"

if [ $# -lt 2 ]; then
  echo "Uso: $0 <name> \"<Display Name>\""
  echo "  name: slug lowercase (ex: contador, financeiro)"
  echo "  Display Name: como aparece na UI (ex: \"Contador\", \"Financeiro\")"
  exit 1
fi

NAME="$1"
DISPLAY_NAME="$2"
AGENTS_YAML="instance/agents/agents.yaml"

# Validacao do nome
if ! [[ "$NAME" =~ ^[a-z][a-z0-9-]{0,30}$ ]]; then
  echo "ERRO: name deve ser lowercase, comecar com letra, so [a-z0-9-]. Recebido: $NAME" >&2
  exit 1
fi

# Checa se ja existe
if grep -qE "^  - name: $NAME\$" "$AGENTS_YAML" 2>/dev/null; then
  echo "ERRO: agente '$NAME' ja existe em $AGENTS_YAML" >&2
  exit 1
fi

if [ ! -f "$AGENTS_YAML" ]; then
  echo "ERRO: $AGENTS_YAML nao existe. Use framework/examples/agents.yaml.example como base." >&2
  exit 1
fi

# Acrescenta entry minima (so o essencial; usuario ajusta depois)
cat >> "$AGENTS_YAML" <<EOF

  - name: $NAME
    display_name: "$DISPLAY_NAME"
    description: "TODO: descreva o papel deste agente em uma linha."
    streams: [$NAME]
    pool_size: 2
    idle_timeout_sec: 900
    write_access: [company]
    read_access: []
    allowed_tools:
      - Read
      - Write
      - Edit
      - Glob
      - Grep
      - mcp__ai_company__ask_human
      - mcp__ai_company__complete_phase
EOF

echo "✓ entry '$NAME' adicionada em $AGENTS_YAML"
echo ""
echo "Edite os campos (description, write_access, allowed_tools, etc) antes de aplicar."
echo "Quando estiver pronto, rode:"
echo "  ./framework/scripts/reconcile.sh"
echo ""
echo "Pra aplicar direto agora (com os defaults), responda 'y':"
read -r -p "Aplicar agora? (y/N) " APPLY
if [ "$APPLY" = "y" ] || [ "$APPLY" = "Y" ]; then
  ./framework/scripts/reconcile.sh
fi
