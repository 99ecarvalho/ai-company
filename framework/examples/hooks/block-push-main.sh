#!/bin/bash
# block-push-main.sh — Claude Code PreToolUse hook.
#
# Bloqueia tentativas do agente fazer `git push` contra main/master.
# Agentes devem empurrar pra branch (ex: task/<slug>) e abrir MR/PR.
#
# Referenciar em agents.yaml via hooks_defaults:
#   hooks_defaults:
#     PreToolUse:
#       - matcher: "Bash"
#         hooks:
#           - type: command
#             command: "/app/hooks/block-push-main.sh"
#
# Requisitos:
# - Instalado em /app/hooks/ via bind mount (D-60). Usa `jq` (presente na
#   imagem agent).
# - NAO ha escape por env var (o agente controlaria) — se precisa push direto
#   em main, humano roda do host (zero impacto no host) ou edita temporariamente
#   hooks_defaults + reconcile + restart.
#
# Saida:
#   exit 0 → deixa passar
#   exit 2 → bloqueia, stderr vira feedback pro Claude
#
# Heuristica: parseia tool_input.command, procura "git push" + alvo main/master.
# Cobre: `git push`, `git push origin main`, `git push origin HEAD:main`,
#        `cd foo && git push`, compound com `;`/`&&`/`||`.
# Nao cobre: push via script escrito pelo agente, alias customizado, subprocess
#            fora de Bash MCP. Esses casos sao raros no workflow padrao.

set -euo pipefail

INPUT=$(cat)
CMD=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

if [[ -z "$CMD" ]]; then
  exit 0
fi

# Quebra o comando em subcommands simples pra analisar cada um. Separadores
# shell-level que iniciam novo comando: ; && || | &  (& em background tambem).
# Tr substitui pra newline, dai lemos linha a linha.
SUBS=$(echo "$CMD" | tr ';|&' '\n')

while IFS= read -r sub; do
  # Trim + ignora vazio
  sub="$(echo "$sub" | sed -E 's/^[[:space:]]+|[[:space:]]+$//g')"
  [[ -z "$sub" ]] && continue

  # Procura "git push" — permite flags+valores entre git e push
  # (ex: `git -C /tmp push`, `git --no-pager push`, `git -c key=val push`).
  # Qualquer token sem separador shell (;|&) pode aparecer.
  if ! echo "$sub" | grep -qE '(^|[[:space:]])git([[:space:]]+[^[:space:]]+)*[[:space:]]+push([[:space:]]|$)'; then
    continue
  fi

  # Achou um git push. Agora vê se o alvo é main/master. Padrões:
  #   git push                              → default branch (pode ser main) — bloqueia por precaucao
  #   git push origin                       → default — bloqueia
  #   git push origin main                  → bloqueia
  #   git push origin master                → bloqueia
  #   git push origin HEAD:main             → bloqueia
  #   git push origin HEAD:refs/heads/main  → bloqueia
  #   git push origin task/foo              → passa
  #   git push origin HEAD:task/foo         → passa
  #   git push --delete origin main         → passa (delete de branch nao eh merge)

  # Delete: libera.
  if echo "$sub" | grep -qE '(^|[[:space:]])(--delete|-d)([[:space:]]|$)'; then
    continue
  fi

  # Extrai args depois de "push". Usa sed pra pegar tudo apos o primeiro "push".
  ARGS=$(echo "$sub" | sed -E 's/^.*[[:space:]]push([[:space:]]|$)//')

  # Se nao ha args explicitos, git usa upstream/default — bloqueia por seguranca.
  if [[ -z "$ARGS" ]] || echo "$ARGS" | grep -qE '^-[^[:space:]]*([[:space:]]|$)'; then
    # Sem refspec visivel OU so flags. Nao da pra ter certeza que alvo nao eh main.
    if [[ -z "$ARGS" ]]; then
      echo "✗ block-push-main: 'git push' sem refspec explicito — pode ir pra main via upstream." >&2
      echo "  Use: git push origin HEAD:task/<slug>" >&2
      exit 2
    fi
  fi

  # Procura tokens main/master como refspec. Padroes que casam:
  #   main | master | <src>:main | <src>:master | <src>:refs/heads/main | refs/heads/main
  if echo "$ARGS" | grep -qE '(^|[[:space:]:])((refs/heads/)?(main|master))([[:space:]]|$)'; then
    echo "" >&2
    echo "✗ block-push-main: push direto em main/master bloqueado." >&2
    echo "  Crie uma branch (ex: task/<slug>), faca push dela e abra MR/PR:" >&2
    echo "    git push origin HEAD:task/<slug>" >&2
    echo "    glab mr create --target-branch main   # ou: gh pr create --base main" >&2
    echo "" >&2
    exit 2
  fi
done <<< "$SUBS"

exit 0
