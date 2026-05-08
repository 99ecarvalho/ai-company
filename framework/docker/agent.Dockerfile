# Imagem base compartilhada por todos os agentes.
# A diferenca entre secretario/dev/revisor/etc. eh apenas env + volume no compose.
FROM node:20-slim

ARG DEBIAN_FRONTEND=noninteractive

# Dependencias de sistema:
# - python3 + venv: runtime do agent_framework (Fase 1+)
# - git + openssh-client: pro dev fazer operacoes em repos/ (Fase 4+)
# - gosu: drop-privilege do root (entrypoint) para node (user final)
# - tzdata: respeitar TZ
# - iproute2 + ca-certificates + curl: utilitarios de rede/debug
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    git openssh-client \
    gosu tzdata \
    iproute2 ca-certificates curl gnupg gzip \
    jq \
    && rm -rf /var/lib/apt/lists/*

# yq (mikefarah/yq) — parser de YAML em CLI, util pra ler manifests como
# agents.yaml/workflows.yaml sem shell-fu. Imagem estatica.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) yq_arch="amd64" ;; \
      arm64) yq_arch="arm64" ;; \
      *) echo "arch nao suportada pra yq: $arch" >&2; exit 1 ;; \
    esac; \
    yq_v="4.44.3"; \
    curl -fsSL -o /usr/local/bin/yq \
      "https://github.com/mikefarah/yq/releases/download/v${yq_v}/yq_linux_${yq_arch}"; \
    chmod 0755 /usr/local/bin/yq; \
    yq --version

# glab (GitLab CLI) + gh (GitHub CLI) — usados pra abrir MR/PR ao fim de cada
# task. Instalados em paralelo pra manter o framework agnostico — a mesma
# imagem de agente serve instancias que usam GitLab ou GitHub.
# glab so distribui .deb/.apk em amd64 e .tar.gz em arm64 — branches distintos.
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
      *) echo "arch nao suportada: $arch" >&2; exit 1 ;; \
    esac; \
    glab --version; \
    # gh (tar.gz disponivel em ambas as arch)
    curl -fsSL -o /tmp/gh.tgz \
      "https://github.com/cli/cli/releases/download/v${gh_v}/gh_${gh_v}_linux_${gh_arch}.tar.gz"; \
    tar -xzf /tmp/gh.tgz -C /tmp; \
    install -m 0755 "/tmp/gh_${gh_v}_linux_${gh_arch}/bin/gh" /usr/local/bin/gh; \
    rm -rf /tmp/gh.tgz "/tmp/gh_${gh_v}_linux_${gh_arch}" 2>/dev/null || true; \
    gh --version

# PostgreSQL client 16 (precisa bater com server postgres:16-alpine pra pg_dump
# nao reclamar de version mismatch). Usa repo apt.postgresql.org.
RUN install -d /usr/share/keyrings \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI (global, disponivel como `claude`)
RUN npm install -g @anthropic-ai/claude-code

# Python venv para o agent_framework. PATH coloca venv na frente.
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Deps do agent_framework (cached layer)
COPY framework/bots/agent_framework/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Codigo do framework
COPY framework/bots/agent_framework /app/agent_framework_src
RUN pip install --no-cache-dir /app/agent_framework_src

# Framework-fixed system prompt sections (read-only; not editable per-instance).
COPY framework/system_prompts /app/system_prompts

# Orquestrador (reactor + scheduler) embutido na mesma imagem.
# Reactor e scheduler rodam `python3 /app/orchestrator/<script>.py`.
COPY framework/orchestrator /app/orchestrator

WORKDIR /app

# Entrypoint: copia credenciais staging (RO) -> volume, chown, gosu node, exec
COPY framework/docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Diretorio onde o Claude CLI escreve (sessoes, cache). Volume nomeado por agente.
RUN mkdir -p /home/node/.claude && chown -R node:node /home/node/.claude

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python3", "-m", "agent_framework.main"]
