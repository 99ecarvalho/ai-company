# ai-company — A virtual company of Claude Code agents

A self-hosted framework where you define "employees" (Claude Code agents)
in a single YAML file and the system takes care of the rest: internal
messaging (Postgres + HTTP broker), audio transcription (faster-whisper),
text-to-speech (Piper), VAPID push, and a PWA to talk to all of them from
one place.

You don't edit Docker YAML — you edit [instance/agents/agents.yaml](instance/agents/agents.yaml)
and run `make reconcile` (or `./run.sh reconcile`).

Source: <https://github.com/99ecarvalho/ai-company>

> For Claude Code: [CLAUDE.md](CLAUDE.md) is auto-loaded.

---

## Prerequisites

- **Docker Engine 25+** with `docker compose` v2
- **git** (the TTS and transcriber services are git submodules)
- **Claude Code CLI logged in** on the host (run `claude` once → creates `~/.claude/`)
- **Linux or WSL2** (macOS should work)
- **NVIDIA GPU** is an automatic opt-in — `install.sh` detects `nvidia-smi`
  and enables the `docker-compose.gpu.yml` overlay. Without a GPU the
  transcriber runs on CPU.

---

## Install

```bash
git clone --recurse-submodules https://github.com/99ecarvalho/ai-company.git
cd ai-company
./run.sh install          # same as: make install
```

No prompts. The script fetches the submodules, generates `.env` with
random secrets, detects the GPU, builds the images, starts the stack,
waits for `/health` and prints the URL. Open it in the browser — the
`/onboard` wizard handles the rest (push notifications, admin password,
company context, agent generation). Everything that used to be a
terminal prompt now lives in the PWA: **Settings → System** (VAPID,
default stream, password) and **/onboard** (company + agents).

Cloned without `--recurse-submodules`? Run `./run.sh submodules` (or
`make submodules`) to fetch them.

### External services (submodules)

TTS and transcription live in their own repos, checked out as git
submodules under `external/` and built by `docker compose`:

| Path | Repo | Compose service | API |
|---|---|---|---|
| `external/ai-tts` | [ai-tts](https://github.com/99ecarvalho/ai-tts) | `tts` | `POST /synthesize` (Piper, `wav` or `mp3`) |
| `external/ai-transcriber` | [ai-transcriber](https://github.com/99ecarvalho/ai-transcriber) | `transcriber` | `POST /transcribe` (faster-whisper) |

The web container proxies both for the PWA (`/api/tts/synthesize`,
`/api/transcribe-preview`). `./run.sh submodules` fetches the pinned
commits; `./run.sh submodules --latest` moves them to the newest `main`
(commit the new pointers to keep them), then `./run.sh rebuild tts transcriber`.

### Multiple companies on the same host

Edit `.env` before running `make install` (or copy it from
`framework/examples/.env.example`) with:

```
COMPOSE_PROJECT_NAME=ai-company-<company>
WEB_PORT=9091
```

Each folder has its own `instance/`; postgres/volumes/network are
namespaced by Docker. `~/.claude/` is shared across the host (the MAX
plan is 1 per machine).

### BYOI / private registry

If any agent in `agents.yaml` uses an `image:` from a private registry,
do this before `make install`:

```bash
mkdir -p ./.docker && chmod 700 ./.docker
DOCKER_CONFIG=./.docker docker login <registry-url>
echo 'DOCKER_CONFIG=./.docker' >> .env
```

Credentials stay isolated to this company (no collision with other
accounts on the same registry).

### Custom paths

The defaults keep everything in `./instance/*`. To version config
separately, move backups to another disk, or keep repos outside the
framework, edit `AGENTS_DIR` / `COMPANY_DIR` / `REPOS_DIR` / `BACKUPS_DIR` /
`SESSIONS_DIR` / `WORKTREES_DIR` / `HOOKS_DIR` in `.env` before
installing — see the table below.

---

## What a fresh install gives you

`make install` (= sanity checks + submodules + `bootstrap-env.sh` + GPU
detection + build + reconcile + up) leaves your instance **runnable
end-to-end** without a single prompt:

### In `instance/agents/`

- **`agents.yaml`** ← `framework/examples/agents.yaml.example`. Defines **5 agents** covering the shipped workflows:
  - `triager` — first contact + wrap-up.
  - `planner` — designs technical plans (Opus, effort high).
  - `executor` — implements code (Bash + worktrees, Opus, effort high).
  - `reviewer` — reviews code + runs tests.
  - `researcher` — code-free investigation (WebFetch/WebSearch).
  - **`block-push-main` hook declared in `hooks_defaults`** — blocks agents from pushing to `main`/`master` (copy the script into `instance/hooks/`, see [Hooks](#hooks-claude-code)).
- **`<name>/CLAUDE.md`** ← generated per agent by reconcile from the template (identity + description), ready to edit.

### In `instance/company/`

- **`CONTEXT.md`** ← `framework/templates/CONTEXT.md.example`. Skeleton with placeholders to fill in (name, industry, mission, date/currency/language conventions). **Important**: this file is injected into the system prompt of EVERY agent on EVERY invocation — the shared source of truth.
- **`workflows.yaml`** ← `framework/examples/workflows.yaml.example`. **2 workflows** ready to use:
  - `default` (5 steps): `intake` → `plan` → `build` → `review` → `wrap`.
  - `research` (3 steps): `intake` → `investigate` → `wrap`.
- **`philosophy.md`** ← empty stub with a hint pointing to the templates below.

### In `framework/templates/philosophies/` (not copied — you choose)

8 operational philosophy templates you can `cp` into `instance/company/philosophy.md`. Each follows the same skeleton (key principles, task structure, vocabulary, archetypal agents, cadence) so claude_runner injects them consistently:

- `tdd.md` — Test-Driven Development
- `bdd.md` — Behavior-Driven Development
- `sdd.md` — Spec-Driven Development
- `ddd.md` — Domain-Driven Design (bounded contexts → agents)
- `tbd.md` — Trunk-Based Development
- `hexagonal.md` — Hexagonal / Clean Architecture
- `context-management.md` — what-goes-into-the-prompt discipline
- `custom.md` — empty skeleton to write from scratch

Mix several if you like — they aren't canonical, they're starting points.

### Subfolders that are NOT pre-created

`CONTEXT.md` references `company/ideas/`, `company/notes/`, `company/decisions/`, `company/tasks/`. These folders are created **lazily** by the agents the first time the path is used (`write_access` has `company` as rw). No need to create them by hand.

### In `instance/repos/` and `instance/worktrees/`

Empty. You populate `instance/repos/<name>/` by cloning the repos your agents will work on (or point `REPOS_DIR` outside the framework — recommended, to avoid nesting gits in the editor workspace). Agents start brand-new projects with the `init_repo` tool, which creates the repo through the web container (agents mount `repos/` read-only, see [Recommended code flow](#recommended-code-flow-worktree--mrpr--anti-push-main)). `worktrees/` is populated dynamically by the `create_worktree` tool when agents start tasks.

### `.env`

Generated by `bootstrap-env.sh` with random secrets — no prompts. See
the full table in [Configuration (`.env`)](#configuration-env).
Everything shown in PWA Settings → System (VAPID, default stream, admin
password) does **not** live here — it lives in `web.app_settings` (DB).

---

## Configuration (`.env`)

`bootstrap-env.sh` generates it with sensible defaults. Manual editing
is optional — only for advanced scenarios (multi-instance, custom
paths, BYOI, production DB credentials, GitLab/GitHub).
See [framework/examples/.env.example](framework/examples/.env.example).

### Postgres / storage

| Variable | Default | Description |
|---|---|---|
| `POSTGRES_DB` | `ai_company` | Database name. |
| `POSTGRES_USER` | `ai_company` | Postgres user. |
| `POSTGRES_PASSWORD` | _(generated by bootstrap)_ | Password. |

### PWA / auth

| Variable | Default | Description |
|---|---|---|
| `WEB_PORT` | `9090` | Port exposed on the host. Unique per instance on the same host. |
| `ADMIN_EMAIL` | `admin@example.com` | E-mail of the primary human (login). |
| `WEB_AUTH_DEV_BYPASS` | `1` | DEV: implicit admin without cookie/token. Automatically set to `false` in `web.app_settings` when you set a password via `/onboard` or Settings → System. |
| `WEB_COOKIE_SECURE` | _(empty)_ | `1` when serving over HTTPS (cookies marked Secure). |

> **Admin password / default stream / push (VAPID)**: configured from the PWA in `Settings → System` or in the `/onboard` wizard. Persisted in `web.app_settings` (DB).

### Service tokens (internal broker)

| Variable | Default | Description |
|---|---|---|
| `ORCHESTRATOR_TOKEN` | _(generated by bootstrap)_ | Reactor auth against the broker. |
| `SCHEDULER_TOKEN` | _(generated by bootstrap)_ | Scheduler auth against the broker. |
| `AGENT_<NAME>_TOKEN` | _(generated by reconcile)_ | Per-agent token, automatic. Do not edit by hand. |

### Transcriber (ai-transcriber, faster-whisper)

GPU is **opt-in** via `docker-compose.gpu.yml` — add `COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml` to `.env` to reserve the NVIDIA GPU and switch to cuda/large-v3/float16. The defaults run on CPU on any host.

| Variable | Default | Description |
|---|---|---|
| `WHISPER_MODEL` | `small` | Model size. With the GPU layer: `large-v3`. |
| `WHISPER_DEVICE` | `cpu` | `cuda` requires `docker-compose.gpu.yml` to be active. |
| `WHISPER_COMPUTE_TYPE` | `int8` | `float16` on GPU. |
| `WHISPER_LANGUAGE` | _(empty = autodetect)_ | Force a language (`pt`, `en`, etc.). |
| `WHISPER_PRELOAD` | `0` | `1` loads the model at startup instead of on the first request. |
| `TRANSCRIBER_API_KEY` | _(empty)_ | If set, the transcriber requires `Authorization: Bearer <key>`; the web proxy sends it. |
| `TRANSCRIBER_MAX_UPLOAD_MB` | `50` | Upload limit, applied by both the web proxy and the transcriber. |

### TTS (ai-tts, Piper)

| Variable | Default | Description |
|---|---|---|
| `TTS_DEFAULT_VOICE` | `pt_BR-faber-medium` | Voice used when a request names none. The ai-tts image ships this voice. |
| `TTS_PRELOAD_VOICE` | `1` | Load the default voice at startup. |

### Deploy / multi-instance

| Variable | Default | Description |
|---|---|---|
| `TZ` | `America/Sao_Paulo` | Timezone applied to every container. |
| `COMPOSE_PROJECT_NAME` | `ai-company` | Namespace for containers/volumes/networks. **Unique per instance on the same host.** |
| `COMPOSE_PROFILES` | _(empty)_ | Set `tunnel` to start cloudflared automatically on every `compose up`. |
| `DOCKER_CONFIG` | _(empty = `~/.docker`)_ | Point to `./.docker/` for isolated credentials (BYOI with a private registry). |

### Customizable paths (host)

The defaults keep everything in `./instance/`. Point them elsewhere to move state to a separate disk/NAS, version config in its own repo, etc.

| Variable | Default | Description |
|---|---|---|
| `AGENTS_DIR` | `./instance/agents` | Agent config (`agents.yaml`, `<name>/CLAUDE.md`, `schedule.yaml`). |
| `COMPANY_DIR` | `./instance/company` | Company context (`CONTEXT.md`, `workflows.yaml`, `tasks/`, `backlog/`). |
| `REPOS_DIR` | `./instance/repos` | Code repos used by the agents (mounted read-only in agents, D-115). **Recommended outside the framework.** |
| `SESSIONS_DIR` | `./instance/sessions` | Runtime cwd per topic/task for each agent (`<agent>/<topic-slug>/`). |
| `BACKUPS_DIR` | `./instance/backups` | Output of `framework/scripts/backup.sh` and `./run.sh backup`. |
| `HOOKS_DIR` | `./instance/hooks` | Claude Code hook scripts referenced in `hooks_defaults`. |
| `INSTANCE_WEB_DIR` | `./instance/web` | PWA asset overrides (custom icons in `icons/`). |
| `WORKTREES_DIR` | `./instance/worktrees` | Where the `create_worktree` tool materializes worktrees (mounted at `/workspace/worktrees` in agents). Kept outside `REPOS_DIR` so it doesn't pollute `git status`. |

### Credentials consumed by `capability_instances`

Variables read by the instances declared in `capability_instances` in
`agents.yaml`. The instances themselves map `env: { KEY: "${VAR:-}" }`,
so fill in the `VAR`s here only if the corresponding instance exists. See
[Capabilities — templates vs instances](#capabilities--templates-vs-instances)
below for the general mechanism.

**MySQL (template `mysql`)** — fill in if you declared an instance
mapping `MYSQL_HOST/USER/PASS/...` to these vars (e.g. `mysql-production`
mapping `${DB_PROD_*}`).

| Variable | Default | Description |
|---|---|---|
| `DB_PROD_HOST` | _(empty)_ | MySQL host. Use `host.docker.internal` if behind a local SSH tunnel. |
| `DB_PROD_PORT` | `3306` | Port. |
| `DB_PROD_USER` | _(empty)_ | Read-only user (recommended). |
| `DB_PROD_PASS` | _(empty)_ | Password. |
| `DB_PROD_NAME` | _(empty)_ | Database. |

**Sentry (template `sentry`)** — fill in if you declared the `sentry`
instance (or another one pointing to the `sentry` template).

| Variable | Default | Description |
|---|---|---|
| `SENTRY_AUTH_TOKEN` | _(empty)_ | Auth token (same name used by sentry-cli/SDKs). Becomes `SENTRY_ACCESS_TOKEN` in the agent env via the instance mapping. |
| `SENTRY_HOST` | _(empty = sentry.io)_ | Hostname only, if self-hosted. |

### Git push / MR-PR

Injected into every agent by reconcile; consultative agents simply never invoke `git push`. The base image ships `glab` and `gh` — the framework is host-agnostic.

| Variable | Default | Description |
|---|---|---|
| `GITLAB_TOKEN` | _(empty)_ | PAT with scopes `api` + `write_repository`. |
| `GITLAB_HOST` | _(empty = gitlab.com)_ | Self-hosted: `gitlab.company.com`. |
| `GH_TOKEN` | _(empty)_ | GitHub PAT with scope `repo`. |
| `GIT_AUTHOR_NAME` | `ai-company` | Identity in commits created by the agents (and by `init_repo`). |
| `GIT_AUTHOR_EMAIL` | `agents@local` | Same. |

### PWA manifest

Read at runtime; changing it requires `docker compose restart web` (no rebuild).

| Variable | Default | Description |
|---|---|---|
| `PWA_NAME` | `Agents` | Long name on the home screen. |
| `PWA_SHORT_NAME` | `Agents` | Short name (icon). |
| `PWA_DESCRIPTION` | `Multi-agent orchestration` | Description. |
| `PWA_THEME_COLOR` | `#0b1220` | Mobile status bar color. |
| `PWA_BACKGROUND_COLOR` | `#0b1220` | Splash screen color. |
| `PWA_ID` | `/ai-company` | Unique ID so the browser can tell instances apart. |
| `PWA_LANG` | `en` | `pt-BR`, `en`, etc. |
| `PWA_START_URL` | `/` | Initial route when the installed PWA opens. |
| `PWA_ORIENTATION` | `any` | `any`/`portrait`/`landscape`/`portrait-primary`/etc. Only takes effect in standalone mode. |

### Cloudflare tunnel (optional)

Exposes the web container at a public hostname via the Cloudflare edge — automatic HTTPS, no DNS/port-forwarding, works behind NAT.

| Variable | Default | Description |
|---|---|---|
| `CF_TUNNEL_TOKEN` | _(empty)_ | Connector token of the tunnel created in the Cloudflare dashboard. Without it, the service does not start. |

### Other

| Variable | Default | Description |
|---|---|---|
| `TERMINAL_NOTIFY_STREAM` | _(empty)_ | Aggregator stream for terminal notifications (`done`/`halt`/`human_review`). Empty = only posts in the origin conversation. |
| `HIRE_MODEL` | _(empty = CLI default)_ | Model used by the PWA's hire/onboard draft generation. |
| `CLAUDE_CODE_USE_FOUNDRY` / `ANTHROPIC_FOUNDRY_*` | _(empty)_ | Run the Claude CLI through Azure AI Foundry instead of Anthropic directly. Injected into every agent by reconcile. |
| `CLAUDE_MOCK` | _(empty)_ | `1` makes `claude_runner` return a scripted reply without invoking the CLI. Used by the Playwright suite. |
| `CLAUDE_MOCK_REPLY` | _(empty)_ | Customizes the text of the mocked reply. |

---

## Day-to-day use

`./run.sh` at the repo root is the main entry point (`./run.sh help`
lists everything):

| Command | What it does |
|---|---|
| `./run.sh install` | First install: checks, submodules, `.env`, GPU detection, build, reconcile, start. |
| `./run.sh setup` | Fetch submodules and create `.env` + `instance/` without building or starting anything. |
| `./run.sh submodules [--latest]` | Fetch `external/ai-tts` and `external/ai-transcriber` at the pinned commits, or move them to the newest `main`. |
| `./run.sh reconcile [--dry-run]` | Apply `instance/agents/agents.yaml` (bots, streams, override file, containers). |
| `./run.sh start\|stop\|restart [SERVICE...]` | Start/stop the whole stack (volumes are kept) or only the given services. |
| `./run.sh build [SERVICE...]` | Build images (fetches submodules first). |
| `./run.sh rebuild SERVICE...` | Build and recreate services — use after editing code (images copy the source; a plain restart keeps the old code). |
| `./run.sh status` | Container states + health check. |
| `./run.sh logs [SERVICE...]` | Follow logs; agent names work too (`executor` → `agent-executor`). |
| `./run.sh shell AGENT` | Shell in an agent container. |
| `./run.sh psql [ARGS...]` | psql on the stack's database. |
| `./run.sh test [unit\|services\|e2e\|all]` | Agent + web unit tests in `.venv` (default), the ai-tts/ai-transcriber suites, or the Playwright suite. |
| `./run.sh smoke` | Call web, TTS and transcriber on the running stack. |
| `./run.sh backup` | Dump the DB and archive `instance/` + `.env` into `${BACKUPS_DIR}/`. |
| `./run.sh reset` | Wipe runtime state (DB + sessions), keep config (asks you to type `yes`). |

The Makefile targets remain available:

```bash
# Setup
make install              # zero-touch first install
make help                 # list all targets
make submodules           # fetch external/ submodules (pinned); submodules-latest = newest main

# Stack
make up                   # start everything (docker compose up -d)
make down                 # stop (keeps volumes)
make restart              # down + up
make build                # rebuild images (fetches submodules first)
make healthcheck          # sanity check (containers + web + db + transcriber)
make test                 # framework pytest (inside the web container)

# Agents / config
make reconcile            # apply instance/agents/agents.yaml (bots + streams + .env + override + up)
make reconcile-dry        # show what reconcile would do, without applying
make new-agent NAME=x DISPLAY="X"   # scaffold a new agent in agents.yaml
make reset-agent-<name>   # clear an agent's sessions (e.g. make reset-agent-executor)

# Logs
make logs                 # tail the whole stack
make logs-<service>       # logs of one service (e.g. make logs-agent-executor)
make logs-all             # aggregated, pretty-printed JSON + colors per service (LEVEL=error filters)
make logs-errors          # warnings+errors only
make shell-<service>      # bash inside a service (e.g. make shell-agent-executor)

# Database / migrations
make migrate              # apply pending migrations (normally runs by itself when web boots)
make migrate-status       # list applied/pending
make migrate-baseline V=N # mark as applied without running
make tasks-migrate        # idempotent backfill company/tasks/ → Postgres

# Reset
make reset-instance       # wipe DB + sessions keeping config (type `yes` to confirm; = ./run.sh reset)
make reset-instance-yes   # same without the prompt (CI/scripts)
```

In the PWA: **👔** in the sidebar header opens the hire wizard (form +
preview of the YAML/CLAUDE.md generated by Claude). **🔔** enables push.

Push arrives **only** when an agent calls `ask_human` (it is blocked
waiting for you). Normal replies don't trigger push — they show up as a
**discreet dot** in the sidebar until you open them. Conversations with
a pending `ask_human` get **strong highlighting** (amber "needs you"
badge + side border).

---

## Extending agents beyond the basics

Two paths when an agent needs something that isn't in the default
image:

**Complex tools via a side MCP** (`capabilities:` in agents.yaml).
Each agent declares the capabilities it wants; reconcile materializes the
MCP server (Compose sidecar or in-process stdio via npx) and injects the
tools into claude.

### Capabilities — templates vs instances

Capabilities fall into two categories (D-119):

- **Singletons** defined in the framework ([reconcile.py](framework/scripts/reconcile.py)
  `MCP_CAPABILITIES`). Fixed config, one per installation. Suited to
  capabilities without varying creds (e.g. a browser pool). Today:
  - `playwright` — headless Chromium via [@playwright/mcp](https://github.com/microsoft/playwright-mcp).
    HTTP sidecar (one instance serves N agents). Enable with
    `capabilities: [playwright]` + `mcp__playwright__*` in `allowed_tools`.

- **Templates** defined in the framework, **instances** declared in
  `instance/agents/agents.yaml` in the top-level `capability_instances`
  block. Suited to any capability with creds that vary per deploy
  (token, connection URL). Each instance has a unique name, which becomes
  the MCP namespace (`mcp__<name>__*`) and the server name. Allows
  multiple instances of the same template (e.g. `mysql-production` +
  `mysql-staging`) with distinct creds, without leaking instance
  vocabulary into the framework.

  Templates today in [reconcile.py](framework/scripts/reconcile.py)
  (`CAPABILITY_TEMPLATES`):
  - `mysql` — read-only via [@benborla29/mcp-server-mysql](https://github.com/benborla/mcp-server-mysql)
    (in-process stdio via npx). Env keys: `MYSQL_HOST`, `MYSQL_PORT`,
    `MYSQL_USER`, `MYSQL_PASS`, `MYSQL_DB`. Accepts a connection via
    `host.docker.internal` if the DB is behind an SSH tunnel on the host.
  - `sentry` — issues, releases and events via [@sentry/mcp-server](https://www.npmjs.com/package/@sentry/mcp-server)
    (in-process stdio via npx). Env keys: `SENTRY_ACCESS_TOKEN`,
    `SENTRY_HOST` (empty = SaaS sentry.io).

  Example instance (in `agents.yaml`):
  ```yaml
  capability_instances:
    mysql-production:
      template: mysql
      env:
        MYSQL_HOST: "${DB_PROD_HOST:-}"
        MYSQL_PORT: "${DB_PROD_PORT:-3306}"
        MYSQL_USER: "${DB_PROD_USER:-}"
        MYSQL_PASS: "${DB_PROD_PASS:-}"
        MYSQL_DB:   "${DB_PROD_NAME:-}"
    sentry:
      template: sentry
      env:
        SENTRY_ACCESS_TOKEN: "${SENTRY_AUTH_TOKEN:-}"
        SENTRY_HOST:         "${SENTRY_HOST:-}"
  ```
  Agents consume them by name in `capabilities:` (mixing instances +
  singletons; resolved transparently):
  ```yaml
  agents:
    - name: ops
      capabilities: [mysql-production, sentry, playwright]
      allowed_tools:
        - "mcp__mysql-production__*"
        - mcp__sentry__list_issues  # or "mcp__sentry__*"
        - "mcp__playwright__*"
  ```

Rule of thumb: **has varying creds → template; pure compute without
creds → singleton**. For a new capability:
- Singleton: 1 entry in `MCP_CAPABILITIES` (+ Dockerfile if HTTP sidecar).
- Template: 1 entry in `CAPABILITY_TEMPLATES` + N instances in `agents.yaml`.

v1 limitation: **one instance per template per agent** — the MCP
server env vars collide if the same agent tries two instances of the same
template. Future v2: automatic env namespacing (`MYSQL_PRODUCTION__HOST`,
`MYSQL_STAGING__HOST`) + expansion in claude_runner.

**Native CLI via your own image** (`image:` in agents.yaml, aka BYOI).
When the tool is executed via `Bash()` by Claude (`phpstan`,
`terraform`, `kubectl`), it must be on the container's PATH. Build your
image by extending [framework/docker/agent.Dockerfile](framework/docker/agent.Dockerfile)
(it must inherit the Python runner + entrypoint), publish it to a registry,
and point to it:

```yaml
- name: php-reviewer
  image: registry.gitlab.com/acme/my-php-agent:v1
  # ... rest unchanged
```

If the registry is private and you have several accounts, run
`DOCKER_CONFIG=./.docker docker login <registry>` and set
`DOCKER_CONFIG=./.docker` in `.env` (see [BYOI / private registry](#byoi--private-registry)).

**What already ships in the base image** (so you don't reinstall it): `git`,
`openssh-client`, `glab` (GitLab CLI), `gh` (GitHub CLI), `jq`,
`curl`, `postgresql-client-16`, `python3`+`venv`, Node 20, Claude Code
CLI. See [framework/docker/agent.Dockerfile](framework/docker/agent.Dockerfile).

### Recommended code flow: worktree + MR/PR + anti-push-main

The framework is **agnostic** (GitLab or GitHub) and provides the pieces;
the instance decides whether to adopt the whole pattern or part of it.
Recommended:

1. **Repos are read-only for agents (D-115).** Agent containers mount
   `repos/` read-only, with only each repo's git dir writable (plus a
   read-write `<repos>/.gitdirs`), so an agent can't edit code in the
   canonical checkout, even via Bash. To start a brand-new project, the
   agent calls the `init_repo(name)` MCP tool: the web container creates
   `repos/<name>/` with an empty initial commit on `main` (`POST /api/repos/init`,
   [framework/web/app/repos.py](framework/web/app/repos.py)), keeping the
   git dir in `<repos>/.gitdirs/<name>.git` so it is writable from agents
   started before the repo existed.
2. **Isolate parallel tasks in git worktrees.** The agent calls the MCP tool
   `create_worktree(task_slug, repo)`. The framework creates the worktree in
   `${WORKTREES_DIR}/<repo>/<task_slug>/` (`/workspace/worktrees` in the
   container, **outside** the tree of the canonical repo in `repos/<repo>/` —
   worktrees never pollute the main repo's `git status`), with branch
   `task/<task_slug>` and baseline at `origin/<default-branch>` HEAD (override
   via args). State is persisted in `tasks.worktrees`; the call is idempotent
   (same task + repo + branch returns the existing worktree). The worktrees
   directory is read-write only for agents with `repos` in `write_access`
   (or `worktree_access: rw`, e.g. a reviewer that runs tests); other agents
   mount it read-only, so they can read a task's code but not change it.
   Cleanup at the
   end of the task via `cleanup_worktrees(task_slug)` — removes via
   `git worktree remove` + `prune` + `DELETE FROM tasks.worktrees`.
3. **Never push directly to `main`.** A Claude Code PreToolUse hook
   blocks `git push origin main|master` when called by the agents.
   Generic script in `framework/examples/hooks/block-push-main.sh`
   — copy it to `${HOOKS_DIR:-instance/hooks}/` and reference it in
   `hooks_defaults` in `agents.yaml`. The block does **not** affect pushes
   from the host (the human pushes freely); it only intercepts Bash coming
   from the agents.
4. **Open an MR/PR at the end.** The agent detects the host via `git remote -v`:
   GitLab → `glab mr create --target-branch main ...`, GitHub →
   `gh pr create --base main ...`. Merging is a human decision.

Tokens needed in `.env`: `GITLAB_TOKEN` (scope `api` +
`write_repository`) or `GH_TOKEN` (scope `repo`), plus
`GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL`. Injected into every agent by
reconcile — only the ones that actually need them (executors, reviewer)
use them.

---

## Hooks (Claude Code)

[Claude Code hooks](https://code.claude.com/docs/en/hooks-guide)
are shell commands that run at lifecycle points (`PreToolUse`,
`PostToolUse`, `Stop`, `SessionStart`, `UserPromptSubmit`, etc.). The
framework exposes them as a **passthrough**: you declare hooks in
`agents.yaml`, reconcile materializes them into a per-agent
`.claude/settings.json`, and the Claude CLI consumes them — the framework
doesn't interpret their semantics.

### Where to declare

Two levels, concatenated per event:

```yaml
# agents.yaml

# Applies to ALL agents (top-level)
hooks_defaults:
  PreToolUse:
    - matcher: "Bash"
      hooks:
        - type: command
          command: "/app/hooks/block-push-main.sh"

agents:
  - name: executor
    # ...
    hooks:                          # Per-agent, added to the defaults
      PreToolUse:
        - matcher: "Bash"
          hooks:
            - type: command
              command: "/app/agents/executor/hooks/block-rm-rf.sh"
      Stop:
        - hooks:
            - type: command
              command: "/app/agents/executor/hooks/notify-finish.sh"
```

### Where to put the scripts

Two possible locations (scripts must be visible **inside** the container):

| Host location | Container path | When to use |
|---|---|---|
| `${HOOKS_DIR:-./instance/hooks}/*.sh` | `/app/hooks/` (ro, all agents) | Shared policy hooks (blocks, audit log, global formatters). |
| `${AGENTS_DIR}/<name>/hooks/*.sh` | `/app/agents/<name>/hooks/` (rw for that agent) | Hooks specific to one role/agent. |

The base image already ships `jq` and `yq` — just `chmod +x` the script.

### Hook contract

Stdin receives a JSON from Claude Code describing the event (with `tool_name`, `tool_input`, etc.). The exit code controls the outcome:

- `exit 0` — let it through (the tool runs normally).
- `exit 2` — **blocks** the tool. The stderr content goes back to Claude as feedback (it becomes a visible message in the context, not a silent error).

Practical example shipped in [framework/examples/hooks/block-push-main.sh](framework/examples/hooks/block-push-main.sh): blocks `git push origin main|master` when it comes from the agent; allows pushes to feature branches. Covers compound commands (`cd foo && git push`), `git -C path push`, `HEAD:main`, etc. Doesn't affect the human's pushes from the host.

### Applying hook changes

Edit `agents.yaml` → `make reconcile` → restart the agent (`docker compose restart agent-<name>`). **No image rebuild** — hooks only regenerate `.claude/settings.json`.

---

## Workflows (multi-step tasks)

Workflows declare the **lifecycle of a multi-agent task**: which
steps exist, which agent runs each one, which artifact each step
produces, and which transitions are valid. They live in
`${COMPANY_DIR}/workflows.yaml` and are **hot-reloaded on every `complete_phase`**
— editing doesn't require a restart.

### Philosophy: agnostic framework, the instance declares the vocabulary

The framework knows **only 3 terminals** with fixed semantics:

| Terminal | Resulting status | When to use |
|---|---|---|
| `done` | `done` | Success. Task closed. |
| `halt` | `blocked` | A human needs to unblock it (e.g. expired token, pending decision). |
| `human_review` | `human_review` | A human needs to pick a direction (e.g. 3 valid paths, no objective answer). |

**Any other name is a free step** declared by your instance. The shipped examples use `intake`/`plan`/`build`/`review`/`wrap`, but you choose — it can be `discovery`/`design`/`ship`, `triage`/`develop`/`merge`, etc.

### Schema of each step

```yaml
workflows:
  default:                  # workflow name — referenced in complete_phase
    initial_step: intake    # first step when the task is created
    steps:
      intake:
        agent: triager      # default executor (optional — without it, complete_phase
                            # requires an explicit `next_agent` when pointing here)
        artifact: 00-intake.md   # name of the file this step produces
                                 # (injected into the next agent's prompt:
                                 #  "look for X in the task")
        next: [plan, halt, human_review]  # valid transitions. complete_phase
                                          # rejects destinations outside this list.
        instructions: |              # (optional) markdown injected into the
          ## What to do here         #  agent's system prompt DURING this step.
          1. Read the task...        #  All phase mechanics live here (gates,
          2. Classify...             #  worktree, push, wrap-up) — not in the
                                     #  agents' CLAUDE.md.
        overrides:                   # (optional) overrides the agent config
          model: sonnet              #  only during this step. Useful for triage
          effort: low                #  on Sonnet/low and execution on Opus/high
          memory:                    #  without duplicate agents.
            auto_inject_limit: 3
      plan:
        agent: planner
        artifact: 01-plan.md
        next: [build, halt, human_review]
      # ...
```

### How a task is bound to a workflow

On the **first call** to `complete_phase` for a new task, the agent passes `workflow: <name>` + `title`. The framework persists it in `tasks.tasks.workflow`; from then on, every `complete_phase` reads this file to:

1. **Validate the transition** (is `next` eligible?).
2. **Resolve the next step's agent** (from the `agent` field, or from an explicit `next_agent`).
3. **Load `instructions` + `overrides`** to build the system prompt of the next turn.

### Permissive mode (no `workflows.yaml` or workflow not found)

The framework **accepts any step** but requires an explicit `next_agent` on every `complete_phase`. Useful to start simple before declaring workflows formally.

### Example workflows

`framework/examples/workflows.yaml.example` is copied to `instance/company/workflows.yaml` at bootstrap. It defines:

- **`default`** (5 steps): `intake` → `plan` → `build` → `review` → `wrap` → `done`. Typical linear flow, with an optional `review` → `build` loop if fixes are needed.
- **`research`** (3 steps): `intake` → `investigate` → `wrap` → `done`. For questions that end in a document, with no code.

The referenced agents (`triager`, `planner`, `executor`, `reviewer`, `researcher`) are also defined in `agents.yaml.example`, so the setup is runnable end-to-end with no adjustments.

### Visual editor in the PWA

- **Settings → Workflows**: edits per-step `instructions` (YAML textarea), `overrides`, `next`, etc.
- **Settings → Preview**: simulates the system prompt assembly for a specific step/agent — useful to see the final concatenation before testing with a real task.

Changes made via the PWA apply on the agent's **next spawn** (no rebuild).

---

## Framework MCP tools

Every agent runs an **in-process** MCP server that exposes the tools below
under the `mcp__ai_company__*` prefix. Each tool appears to the agent as
a Claude function call — enabling one = listing it in `allowed_tools` in
`agents.yaml` (or inheriting the image default). **Capability** tools
(playwright/sentry/mysql) are separate and described in [Extending agents](#extending-agents-beyond-the-basics).

Instead of listing each tool on every agent, `agents.yaml` accepts tool
groups written as `group:<name>`, and a top-level `allowed_tools_defaults`
that every agent inherits (opt out per agent with `inherit_tool_defaults: false`):

```yaml
allowed_tools_defaults: [group:files, group:human, group:agents, group:workflow, group:memory]
agents:
  - name: executor
    allowed_tools: [Bash, group:worktree]   # only what it needs on top of the defaults
```

Built-in groups: `files` (Read/Write/Edit/Glob/Grep), `web` (WebFetch/WebSearch),
`human`, `agents`, `workflow` (complete_phase, get_task_state), `tasks`,
`worktree`, `memory`, `backlog`, `scheduler`, `skills` — the tables below are
grouped the same way. Define your own (or override one) under `tool_groups:`.
reconcile expands everything into the agent's `.claude/settings.json`. Keep
tool names and framework mechanics out of agents' `CLAUDE.md`: reconcile
warns when it finds them there (the platform rules already explain the tools).

### Human ↔ agent communication

| Tool | What it does |
|---|---|
| `ask_human` | Asks the human and **blocks** indefinitely until answered. Use for decisions that change scope/architecture or ambiguities with no objective answer. The PWA highlights the conversation with a "needs you" badge. |
| `notify_human` | Posts a message in the current conv (fire-and-forget). Doesn't block or demand attention — it shows up as a discreet dot in the sidebar. |
| `archive_conversation` | Archives the current conv (leaves the inbox; history preserved, can be reopened). |

### Coordination between agents

| Tool | What it does |
|---|---|
| `ask_agent` | Pauses and asks ANOTHER agent. Creates a child conv; the target agent receives the question as a prompt and the asker waits for the synchronous answer. Root→child hierarchy enforced by the broker. Requires `can_ask: [<target>]` in `agent_policies`. |
| `ask_agents_many` | Several `ask_agent`/dispatches in **parallel** in a single call. Useful for fan-out (consulting 3 specialists at once). |

### Task lifecycle

| Tool | What it does |
|---|---|
| `complete_phase` | Marks the current step as completed and hands off. `next=<next-step>` continues the task; `next=done`/`halt`/`human_review` is terminal (the orchestrator changes the status). |
| `get_task_state` | Reads the current task's structured state: slug, title, workflow, status, completed phases, active worktrees, baselines. Replaces the ritual of inspecting metadata.yaml. |
| `task_list` | Lists active tasks (default) or includes archived ones. Filters by status/agent. |
| `reopen_task` | Reopens a task in a terminal state (done/blocked/human_review). Restriction: only when the human explicitly asks. |

### Repos and worktrees

| Tool | What it does |
|---|---|
| `init_repo` | Creates a new git repo at `/workspace/repos/<name>/` with an empty initial commit on `main`, through the web container (`POST /api/repos/init`). Idempotent: an existing repo is returned as is. Call `create_worktree` next. |
| `create_worktree` | Creates an isolated worktree in `${WORKTREES_DIR}/<repo>/<task_slug>/` + records it in `tasks.worktrees`. Idempotent (same task+repo+branch reuses it). Default branch: `task/<slug>`. Default baseline: `origin/<default-branch>` HEAD. |
| `cleanup_worktrees` | Removes every worktree recorded for the task: `git worktree remove --force` + `prune` + row delete. Failures are not masked. |

### Persistent memory (per agent)

| Tool | What it does |
|---|---|
| `memory_save` | Saves a long-term fact in **this** agent's memory (not shared between agents). Tag required, key optional. |
| `memory_recall` | Semantic + text search over the agent's memory. Ordered by relevance, with `limit`. |
| `memory_list` | Lists recent facts without a query — useful to audit what has accumulated. |
| `memory_edit` | Updates an existing fact (fails if the key doesn't exist). |
| `memory_delete` | Removes a fact (hard delete; no undo). |

### Backlog (shared queue of ideas/pending items)

| Tool | What it does |
|---|---|
| `backlog_add` | Records an idea/pending item. Shared between agents (not per agent). |
| `backlog_list` | Lists ordered by priority desc, then created_at. Filters by status/owner. |
| `backlog_update` | Updates fields of an existing item; omitted fields keep their value. |
| `backlog_promote` | Promotes an item → active task. Creates the task with the declared workflow and dispatches the initial phase. |

### Scheduler (DB-backed cron jobs)

| Tool | What it does |
|---|---|
| `schedule_add` | Creates a recurring job (cron expression). Creates its own conv on every run. |
| `schedule_list` | Lists custom jobs created via MCP/PWA (doesn't include native overrides defined in YAML). |
| `schedule_update` | Edits an existing job; omitted fields are preserved. |
| `schedule_remove` | Deletes a job permanently. |

### Skills (per-agent Claude Code skills)

| Tool | What it does |
|---|---|
| `save_skill` | Persists a skill in **this** agent's library (`/app/agents/<name>/skills/`). Each agent has its own. |
| `list_skills` | Lists the agent's skills (name + description). |
| `delete_skill` | Removes a skill (hard delete). |

---

## Architecture (summary)

- **Agents** = Python containers with a `claude -p` subprocess + in-process
  MCP server (`ask_human`, `ask_agent`, `complete_phase`, `memory_*`, ...).
  They mount `repos/` read-only (D-115) and edit code only in worktrees.
- **web** = FastAPI + SvelteKit PWA (bundle ~300KB, zero Node at runtime).
  HTTP broker over Postgres. Also creates repos for agents
  (`/api/repos/init`) and proxies TTS/transcription for the PWA.
- **Postgres** = single storage. `pg_notify` triggers LISTEN in the agents.
  Schema versioned in [framework/db/migrations/](framework/db/migrations/);
  the runner applies pending ones in the `web` entrypoint (zero manual steps).
- **orchestrator-reactor + scheduler** = handoffs and cron jobs.
- **transcriber** = [ai-transcriber](https://github.com/99ecarvalho/ai-transcriber)
  (faster-whisper, GPU or CPU), submodule in `external/ai-transcriber`.
- **tts** = [ai-tts](https://github.com/99ecarvalho/ai-tts) (Piper, CPU),
  submodule in `external/ai-tts`.
- **watchdog** = monitors `instance/heartbeats/` and restarts stale agents.
- **Side MCPs** (opt-in via `capabilities:`) — two categories (D-119):
  - **Singletons** (HTTP sidecar): `playwright-mcp` (automated browser,
    shared container).
  - **Parameterizable templates** (in-process stdio via npx, instances
    declared in `capability_instances` in `agents.yaml`): `mysql`
    (read-only via `@benborla29/mcp-server-mysql` — accepts `host.docker.internal`
    if the DB is behind an SSH tunnel on the host) and `sentry`
    (`@sentry/mcp-server`). Each instance becomes `mcp__<name>__*` in the
    MCP namespace. See [Capabilities — templates vs instances](#capabilities--templates-vs-instances)
    for details.

---

## Troubleshooting

**Claude CLI expired.** Run `claude` on the host → re-authenticate. Agents
pick it up on the next run (read-only bind mount of `~/.claude/`).

**No GPU.** The default transcriber config is CPU (model `small`,
`int8`); the installer only adds the GPU layer when it finds `nvidia-smi`.
To turn transcription off entirely, remove the `transcriber` service
from [docker-compose.yml](docker-compose.yml).

**Empty `external/` / build fails on tts or transcriber.** The submodules
weren't fetched: `./run.sh submodules`.

**Push blocked.** The browser refused permission. Settings → site → allow
notifications → click 🔔 again.

**Nuclear reset.** `docker compose down -v` wipes the whole Postgres
(messages, memory, telemetry, push subs). On the next `up`, the
`web` entrypoint runs the migrations from scratch.

**New schema migration.** Create `framework/db/migrations/NNN_desc.sql`,
rebuild `web` (`./run.sh rebuild web`). The entrypoint applies it.
`make migrate-status` lists applied/pending.

---

## Project structure

```
ai-company/
├── framework/            # tracked CODE
│   ├── bots/             # agent runner (Python asyncio + MCP), package ai_company
│   ├── web/              # FastAPI + SvelteKit (broker, PWA, repo creation)
│   ├── orchestrator/     # reactor + scheduler
│   ├── watchdog/         # heartbeat watchdog + dev hot-reload
│   ├── db/migrations/    # versioned schema (NNN_name.sql)
│   ├── docker/           # Dockerfiles (agent, playwright-mcp)
│   ├── scripts/          # install, reconcile, bootstrap-env, migrate
│   ├── system_prompts/   # platform.md (framework invariants for agents)
│   ├── templates/        # CONTEXT.md and philosophy templates
│   ├── agent-template/   # skeleton for new agent folders
│   └── examples/         # generic defaults copied at bootstrap
├── external/             # git submodules: ai-tts, ai-transcriber
├── instance/             # gitignored STATE (agents.yaml, company/, ...)
├── CLAUDE.md             # auto-loaded by Claude Code
├── run.sh                # main entry point (install, run, test, operate)
├── Makefile
└── docker-compose.yml
```

Rule: nothing in `instance/` goes into the repo. `make install` populates
`instance/` from `framework/examples/`. Repos used by the agents can live
in `instance/repos/` (default) or outside (via `REPOS_DIR` in `.env`) —
**outside is recommended** to avoid nesting N gits in the editor
workspace.

---

## Docs

- [CLAUDE.md](CLAUDE.md) — context auto-loaded by Claude Code (structure, rules, agents' system prompt, quick access).
- [framework/web/frontend/MOBILE.md](framework/web/frontend/MOBILE.md) — mobile layout/UI pitfalls and PWA cache.
