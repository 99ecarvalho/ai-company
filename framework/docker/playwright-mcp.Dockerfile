# Playwright MCP server — serve tools de automacao de browser via MCP
# aos agentes que declaram `capabilities: [playwright]` em agents.yaml.
#
# Uma instancia serve N agentes (lateral, nao acopla com imagem do agente).
# Expoe SSE em :8931. Agentes apontam mcp.extra.json pro endpoint /sse.
FROM node:20-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Versao do @playwright/mcp pinada (evita drift entre build e runtime).
# Pra atualizar: bump aqui + rebuild da imagem.
ARG PLAYWRIGHT_MCP_VERSION=0.0.73

# Instala o MCP server globalmente E baixa o chromium na MESMA versao do
# `playwright-core` que o pacote traz transitivamente. Detalhes do gotcha:
#
# - `npm install -g @playwright/mcp@<X>` resolve uma versao de playwright-core
#   especifica como dep transitiva (ex: 1.60.0-alpha-...).
# - `npx -y playwright install chromium` (pacote standalone, sem versao)
#   pega `playwright@latest` que pode estar em versao DIFERENTE do
#   playwright-core embarcado. Resultado: baixa chromium-NNNN no .cache,
#   mas o playwright-core embarcado procura chromium-MMMM em runtime e
#   levanta "Executable doesn't exist".
# - Fix: detectar a versao exata de playwright-core embarcada e instalar
#   chromium usando `playwright@<MESMA-VERSAO>` como CLI.
#
# `--with-deps` traz libs de sistema (libnss, fonts, etc) via apt.
# Cleanup limita a /root/.cache/npm — `rm -rf /root/.cache` apagaria
# /root/.cache/ms-playwright (binarios chromium recem-baixados).
RUN npm install -g @playwright/mcp@${PLAYWRIGHT_MCP_VERSION} \
    && PW_CORE_VERSION=$(node -p "require('/usr/local/lib/node_modules/@playwright/mcp/node_modules/playwright-core/package.json').version") \
    && echo "playwright-core embedded version: ${PW_CORE_VERSION}" \
    && npx -y playwright@${PW_CORE_VERSION} install chromium --with-deps \
    && rm -rf /root/.npm /root/.cache/npm

# Forca uso do chromium bundled pelo playwright (vs Google Chrome stable,
# que e o default do @playwright/mcp e exigiria /opt/google/chrome/chrome).
# CLI flag `--browser` so aceita channels (chrome/firefox/webkit/msedge),
# nao "chromium" — entao passamos via config file.
#
# chromiumSandbox: false — chromium recusa rodar como root sem sandbox
# (proteção contra escalonamento). Imagem oficial do node:20-slim roda
# como root e nao tem usuario non-root configurado. Como o container vive
# na rede compose interna (sem exposicao externa) e processa apenas
# requests dos agentes do framework, desligar o sandbox aqui e aceitavel.
# Alternativa "certa" seria criar usuario nao-root + ajustar perms — fica
# como melhoria futura se houver hardening.
RUN mkdir -p /etc/playwright-mcp \
    && printf '%s\n' '{' \
        '  "browser": {' \
        '    "browserName": "chromium",' \
        '    "launchOptions": { "chromiumSandbox": false }' \
        '  }' \
        '}' > /etc/playwright-mcp/config.json

EXPOSE 8931

# Usa o bin global instalado acima (`playwright-mcp` em /usr/local/bin/) —
# NAO `npx -y @playwright/mcp@latest`, que toda execucao baixa a versao
# mais nova do npm e desalinha com o chromium baixado em build.
#
# --config: forca browserName=chromium (ver bloco anterior).
# --headless + --isolated: sem UI, contexto limpo por sessao.
# --host 0.0.0.0 pra aceitar conexao da rede compose interna.
# --allowed-hosts '*' pra aceitar Host: playwright-mcp (DNS interno do
# compose) alem do 'localhost' default. Seguro — rede compose e interna.
CMD ["playwright-mcp", \
     "--config", "/etc/playwright-mcp/config.json", \
     "--host", "0.0.0.0", \
     "--port", "8931", \
     "--allowed-hosts", "*", \
     "--headless", \
     "--isolated"]
