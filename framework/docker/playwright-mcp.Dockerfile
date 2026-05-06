# Playwright MCP server — serve tools de automacao de browser via MCP
# aos agentes que declaram `capabilities: [playwright]` em agents.yaml.
#
# Uma instancia serve N agentes (lateral, nao acopla com imagem do agente).
# Expoe SSE em :8931. Agentes apontam mcp.extra.json pro endpoint /sse.
FROM node:20-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Pre-baixa o MCP server + deps do Chromium (evita download no primeiro request).
# --with-deps traz libs de sistema (fonts, libnss, etc).
RUN npm install -g @playwright/mcp@latest \
    && npx -y playwright install chromium --with-deps \
    && rm -rf /root/.npm /root/.cache

EXPOSE 8931

# --headless + --isolated: sem UI, contexto limpo por sessao.
# --host 0.0.0.0 pra aceitar conexao da rede compose interna.
# --allowed-hosts '*' pra aceitar Host: playwright-mcp (DNS interno do compose)
# alem do 'localhost' default. Seguro — rede compose e interna e isolada.
CMD ["npx", "-y", "@playwright/mcp@latest", \
     "--host", "0.0.0.0", \
     "--port", "8931", \
     "--allowed-hosts", "*", \
     "--headless", \
     "--isolated"]
