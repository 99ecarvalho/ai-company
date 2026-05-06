# MCP MySQL server (read-only) — serve tools de query em MySQL aos agentes
# que declaram `capabilities: [mysql-producao]` em agents.yaml.
#
# Padrao igual ao playwright-mcp: uma instancia serve N agentes, expoe
# streamable HTTP em /mcp na porta 8931. Agentes apontam mcp.extra.json
# pro endpoint.
#
# Server base: @benborla29/mcp-server-mysql (stdio). Read-only por default
# (ALLOW_INSERT_OPERATION/ALLOW_UPDATE_OPERATION/ALLOW_DELETE_OPERATION nao
# setados). supergateway bridga stdio -> streamable HTTP.
#
# Credenciais vem do .env da instance (DB_PROD_HOST/PORT/USER/PASS/NAME),
# injetadas pelo docker-compose.override.yml (gerado pelo reconcile) como
# MYSQL_HOST/MYSQL_PORT/MYSQL_USER/MYSQL_PASS/MYSQL_DB.
FROM node:20-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @benborla29/mcp-server-mysql supergateway \
    && rm -rf /root/.npm /root/.cache

EXPOSE 8931

# supergateway: stdio -> streamable HTTP em :8931/mcp.
# --stdio recebe o comando completo como string unica.
# read-only enforced no mcp-server-mysql (ALLOW_*_OPERATION=false default).
CMD ["supergateway", \
     "--stdio", "npx -y @benborla29/mcp-server-mysql", \
     "--outputTransport", "streamableHttp", \
     "--port", "8931"]
