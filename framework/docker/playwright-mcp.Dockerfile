# Playwright MCP server — serves browser automation tools via MCP to the
# agents that declare `capabilities: [playwright]` in agents.yaml.
#
# One instance serves N agents (sidecar, not coupled to the agent image).
# Exposes SSE on :8931. Agents point mcp.extra.json at the /sse endpoint.
FROM node:20-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Pinned @playwright/mcp version (avoids drift between build and runtime).
# To update: bump it here + rebuild the image.
ARG PLAYWRIGHT_MCP_VERSION=0.0.73

# Install the MCP server globally AND download chromium for the SAME version
# of `playwright-core` the package pulls in transitively. The gotcha:
#
# - `npm install -g @playwright/mcp@<X>` resolves a specific playwright-core
#   version as a transitive dep (e.g. 1.60.0-alpha-...).
# - `npx -y playwright install chromium` (standalone package, unversioned)
#   picks `playwright@latest`, which may be a DIFFERENT version from the
#   bundled playwright-core. Result: chromium-NNNN lands in .cache, but the
#   bundled playwright-core looks for chromium-MMMM at runtime and raises
#   "Executable doesn't exist".
# - Fix: detect the exact bundled playwright-core version and install
#   chromium using `playwright@<SAME-VERSION>` as the CLI.
#
# `--with-deps` pulls system libs (libnss, fonts, etc) via apt.
# Cleanup is limited to /root/.cache/npm — `rm -rf /root/.cache` would delete
# /root/.cache/ms-playwright (the freshly downloaded chromium binaries).
RUN npm install -g @playwright/mcp@${PLAYWRIGHT_MCP_VERSION} \
    && PW_CORE_VERSION=$(node -p "require('/usr/local/lib/node_modules/@playwright/mcp/node_modules/playwright-core/package.json').version") \
    && echo "playwright-core embedded version: ${PW_CORE_VERSION}" \
    && npx -y playwright@${PW_CORE_VERSION} install chromium --with-deps \
    && rm -rf /root/.npm /root/.cache/npm

# Force the chromium bundled with playwright (vs Google Chrome stable,
# which is the @playwright/mcp default and would require /opt/google/chrome/chrome).
# The `--browser` CLI flag only accepts channels (chrome/firefox/webkit/msedge),
# not "chromium" — so we pass it via a config file.
#
# chromiumSandbox: false — chromium refuses to run as root with the sandbox
# (privilege escalation protection). The official node:20-slim image runs
# as root and has no non-root user configured. Since the container lives on
# the internal compose network (no external exposure) and only handles
# requests from the framework's agents, disabling the sandbox here is acceptable.
# The "proper" alternative would be a non-root user + adjusted perms — left
# as a future improvement if hardening is needed.
RUN mkdir -p /etc/playwright-mcp \
    && printf '%s\n' '{' \
        '  "browser": {' \
        '    "browserName": "chromium",' \
        '    "launchOptions": { "chromiumSandbox": false }' \
        '  }' \
        '}' > /etc/playwright-mcp/config.json

EXPOSE 8931

# Use the global bin installed above (`playwright-mcp` in /usr/local/bin/) —
# NOT `npx -y @playwright/mcp@latest`, which downloads the newest npm version
# on every run and drifts from the chromium downloaded at build time.
#
# --config: forces browserName=chromium (see the previous block).
# --headless + --isolated: no UI, clean context per session.
# --host 0.0.0.0 to accept connections from the internal compose network.
# --allowed-hosts '*' to accept Host: playwright-mcp (compose internal DNS)
# besides the default 'localhost'. Safe — the compose network is internal.
CMD ["playwright-mcp", \
     "--config", "/etc/playwright-mcp/config.json", \
     "--host", "0.0.0.0", \
     "--port", "8931", \
     "--allowed-hosts", "*", \
     "--headless", \
     "--isolated"]
