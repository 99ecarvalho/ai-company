# Base image shared by all agents.
# Agents differ only in env + volumes in the compose file.
FROM node:20-slim

ARG DEBIAN_FRONTEND=noninteractive

# System dependencies:
# - python3 + venv: ai_company runtime (Phase 1+)
# - git + openssh-client: for agents to run git operations in repos/ (Phase 4+)
# - gosu: drop privileges from root (entrypoint) to node (final user)
# - tzdata: honor TZ
# - iproute2 + ca-certificates + curl: network/debug utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    git openssh-client \
    gosu tzdata \
    iproute2 ca-certificates curl gnupg gzip \
    jq \
    && rm -rf /var/lib/apt/lists/*

# yq (mikefarah/yq) — CLI YAML parser, handy for reading manifests such as
# agents.yaml/workflows.yaml without shell-fu. Static binary.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) yq_arch="amd64" ;; \
      arm64) yq_arch="arm64" ;; \
      *) echo "unsupported arch for yq: $arch" >&2; exit 1 ;; \
    esac; \
    yq_v="4.44.3"; \
    curl -fsSL -o /usr/local/bin/yq \
      "https://github.com/mikefarah/yq/releases/download/v${yq_v}/yq_linux_${yq_arch}"; \
    chmod 0755 /usr/local/bin/yq; \
    yq --version

# glab (GitLab CLI) + gh (GitHub CLI) — used to open an MR/PR at the end of each
# task. Both are installed to keep the framework agnostic — the same agent
# image serves instances that use GitLab or GitHub.
# glab only ships .deb/.apk on amd64 and .tar.gz on arm64 — separate branches.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    glab_v="1.92.1"; \
    gh_v="2.63.2"; \
    case "$arch" in \
      amd64) \
        curl -fsSL -o /tmp/glab.deb \
          "https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/packages/generic/glab/${glab_v}/glab_${glab_v}_linux_amd64.deb"; \
        apt-get install -y /tmp/glab.deb; \
        rm /tmp/glab.deb; \
        gh_arch="amd64" \
        ;; \
      arm64) \
        curl -fsSL -o /tmp/glab.tgz \
          "https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/packages/generic/glab/${glab_v}/glab_${glab_v}_linux_arm64.tar.gz"; \
        tar -xzf /tmp/glab.tgz -C /tmp; \
        install -m 0755 /tmp/bin/glab /usr/local/bin/glab; \
        rm -rf /tmp/glab.tgz /tmp/bin /tmp/LICENSE* /tmp/README* /tmp/manpages 2>/dev/null || true; \
        gh_arch="arm64" \
        ;; \
      *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    glab --version; \
    # gh (tar.gz available for both archs)
    curl -fsSL -o /tmp/gh.tgz \
      "https://github.com/cli/cli/releases/download/v${gh_v}/gh_${gh_v}_linux_${gh_arch}.tar.gz"; \
    tar -xzf /tmp/gh.tgz -C /tmp; \
    install -m 0755 "/tmp/gh_${gh_v}_linux_${gh_arch}/bin/gh" /usr/local/bin/gh; \
    rm -rf /tmp/gh.tgz "/tmp/gh_${gh_v}_linux_${gh_arch}" 2>/dev/null || true; \
    gh --version

# PostgreSQL client 16 (must match the postgres:16-alpine server so pg_dump
# does not complain about a version mismatch). Uses the apt.postgresql.org repo.
RUN install -d /usr/share/keyrings \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI (global, available as `claude`)
RUN npm install -g @anthropic-ai/claude-code

# Python venv for ai_company. PATH puts the venv first.
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# ai_company deps (cached layer)
COPY framework/bots/ai_company/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Framework code
COPY framework/bots/ai_company /app/ai_company_src
RUN pip install --no-cache-dir /app/ai_company_src

# Framework-fixed system prompt sections (read-only; not editable per-instance).
COPY framework/system_prompts /app/system_prompts

# Orchestrator (reactor + scheduler) bundled in the same image.
# Reactor and scheduler run `python3 /app/orchestrator/<script>.py`.
COPY framework/orchestrator /app/orchestrator

WORKDIR /app

# Entrypoint: copy staging credentials (RO) -> volume, chown, gosu node, exec
COPY framework/docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Directory the Claude CLI writes to (sessions, cache). One named volume per agent.
RUN mkdir -p /home/node/.claude && chown -R node:node /home/node/.claude

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python3", "-m", "ai_company.main"]
