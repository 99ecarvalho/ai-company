# ai-company — auto-loaded context

> Messaging runs on an **internal broker** (Postgres + HTTP in the `web` container).

Self-hosted framework to orchestrate Claude Code agents: each agent runs in a container, listens to streams (HTTP broker in the `web` container, Postgres persistence with `pg_notify`), and uses MCP for `ask_human`, `ask_agent`, `complete_phase`, `init_repo` and persistent memory. Orchestrator (LISTEN reactor + APScheduler scheduler) + transcriber (ai-transcriber, faster-whisper, GPU optional) + TTS (ai-tts, Piper) + web PWA (conversations + voice + VAPID push + telemetry + file viewer/upload) come with the core.

> **CLAUDE.local.md** (gitignored): if it exists, it holds local overrides — autonomous mode, build logs, personal notes for the instance. Read it too if present.

## Repo layout (4 zones)

- **`framework/`** (tracked) — ai-company code: `bots/` (Python package `framework/bots/ai_company/ai_company`), `orchestrator/`, `web/`, `watchdog/`, `db/migrations/`, `docker/`, `scripts/`, `system_prompts/`, `templates/`, `agent-template/`, `examples/`.
- **`external/`** (tracked as git submodules) — `external/ai-tts` ([ai-tts](https://github.com/99ecarvalho/ai-tts), compose service `tts`) and `external/ai-transcriber` ([ai-transcriber](https://github.com/99ecarvalho/ai-transcriber), compose service `transcriber`). Separate repos: change them there, then bump the pointer here. Fetch with `git clone --recurse-submodules`, `make submodules` or `./run.sh submodules [--latest]`.
- **`instance/`** (gitignored) — state/config of the user company: `agents/agents.yaml`, `agents/<name>/`, `company/`, `repos/`, `worktrees/`, `hooks/`, `backups/`, `heartbeats/`, `sessions/<name>/` (runtime cwds per topic, D-51). Paths customizable via `.env` (`AGENTS_DIR`, `COMPANY_DIR`, `BACKUPS_DIR`, `REPOS_DIR`, `SESSIONS_DIR`, `WORKTREES_DIR`, `HOOKS_DIR`) — the default keeps everything in `./instance/`.
- **`.dev/`** (gitignored, optional) — local meta-development: if you keep notes in `notes/PROJECT_PLAN.md`, `notes/DECISIONS.md`, `notes/EXECUTION_LOG.md`, `notes/QUESTIONS.md`, read them before touching the framework.

## Absolute rules

- **Framework, not a fixed app.** The agents' source of truth is `instance/agents/agents.yaml` (gitignored; initialize it with `bash framework/scripts/bootstrap-env.sh` or `./run.sh setup`, which copy the `.example`). `docker-compose.override.yml` and `instance/agents/<name>/agent.yaml` are **generated** by `framework/scripts/reconcile.py`. Never edit them by hand; edit `agents.yaml` + `make reconcile`.
- **Repo = distributable framework.** Nothing specific to a company or user goes into the tracked repo. The whole instance lives in `instance/`, meta-dev in `.dev/`, both gitignored. Tracked defaults (`framework/examples/*`, `framework/agent-template/`, `framework/templates/`) must be generic.
- **Project scope = framework + orchestrator + broker.** The content of `instance/repos/` is the agents' workspace (code produced by agents during tasks) and is **NOT a target for improvement or review**. Same for `external/` from this repo: TTS/transcriber changes belong in their own repos.
- **Platform, not roles.** When proposing features/next steps, **never** suggest agents/roles specific to a type of business. Suggest only platform capabilities (memory, ask_agent, auto-recovery, telemetry, TLS, scaffold, plugin system, etc). Test: "is this item code I write once and that serves any company?" — if not, it's a role; don't suggest it.
- **Instance vocabulary NEVER goes into the framework.** Phase names (e.g. `intake`, `plan`, `build`, `review`, `wrap`) or role names (e.g. `coordinator`, `analyst`, `executor`, `reviewer`) are conventions of the user instance — they are **not** invariants of ai-company. The framework only knows generic primitives: **arbitrary steps** declared by the instance + **semantic terminals** that the orchestrator logic knows (today `done`/`halt`/`human_review`, because those change status/dispatch in the reactor). Smell: an enum/constant/mapping with instance-specific names inside `framework/`. If that happens, move the taxonomy to instance config (e.g. `instance/company/workflows.yaml`) loaded at runtime. Test: "if I hand the framework to a company that uses other phase names, does the code break?" — if yes, it's a mess.
- **Commit often** as you go. No `git push` without explicit authorization.
- **Never touch anything outside the project directory** except strictly necessary reads.
- **Edit Python → rebuild, not restart.** The `agent` and `web` images `COPY` the code at build time (there's no bind mount of the source). `docker compose restart <svc>` restarts the **same binary** — the change on the host **doesn't load**. Correct flow after editing code: `./run.sh rebuild <svc>` (= `docker compose build <svc> && docker compose up -d --force-recreate <svc>`), or `make build` to rebuild all of them. Same after touching [framework/bots/ai_company/](framework/bots/ai_company/): rebuild the `agent` image (affects every agent container).
- **Repos are read-only for agents (D-115).** Agent containers mount `repos/` read-only, with only each repo's `.git/` plus `<repos>/.gitdirs` writable; code is edited only in worktrees (`create_worktree`). New repos are created by the `init_repo` MCP tool through the web container ([framework/web/app/repos.py](framework/web/app/repos.py), `POST /api/repos/init`), with the git dir in `<repos>/.gitdirs/<name>.git`. Don't give agents a write path back into `repos/`. The task worktrees (`/workspace/worktrees`) are read-write only for agents with `repos` in `write_access` or `worktree_access: rw`; read-only for the rest.

## How the agents' system prompt is assembled

`claude_runner._build_system_prompt` ([framework/bots/ai_company/ai_company/claude_runner.py](framework/bots/ai_company/ai_company/claude_runner.py)) builds the `--append-system-prompt` by concatenating, in this order (each section has a toggle in `instance/company/system_prompts/config.yaml`, template in `framework/examples/system_prompts/config.yaml.example`):

1. `system_prompts/platform.md` ([framework/system_prompts/platform.md](framework/system_prompts/platform.md)) — **framework invariants**: honesty, root->child hierarchy (broker 409), reply-as-gateway, MCP tools, baseline_sha discipline, generic memory/skills/overload.
2. `## Invocation mode` (dynamic) — query on `messaging.conversations.parent_conv_id` at runtime: says whether the conv is root (human/cron) or child (`ask_agent` from another agent) + the parent's name. Replaces the "Question from `<X>`" heuristic each agent used to apply by hand.
3. `## Task state` (dynamic, conditional on `topic = task-*`) — snapshot of `tasks.tasks` + `phases` + `worktrees`: slug, title, workflow, current_step, complexity, baselines (`metadata_extra.baseline`), active worktrees, completed phases, origin. Avoids the `get_task_state` ritual at the start of the phase.
4. `## Current phase instructions` (dynamic, conditional) — the `instructions` field (markdown) of the current step in `instance/company/workflows.yaml` -> `workflows.<wf>.steps.<step>.instructions`. **All phase mechanics** (approval gate, worktree, push/MR, post-review loop, wrap-up) live here — not in the agents' CLAUDE.md.

   **Runtime overrides per step (D-113):** the same step can declare `overrides: {model, effort, memory: {enabled, auto_inject_limit}}`. When set, it overrides the agent's config from `agents.yaml` *only during this step*. The workflow is authoritative, no ceiling. Useful for triage on Sonnet/low and execution on Opus/high without duplicate agents. PWA Settings -> Workflows has a dedicated fieldset per step. Hot-reload: editing via the PWA applies on the next spawn, no rebuild.
5. `agents/<name>/CLAUDE.md` — **identity + stack of the role** (languages, repos, out-of-scope, etc). Must not contain framework or phase mechanics — if it does, that's a symptom of drift.
6. `company/CONTEXT.md` — the instance's general catalog (workflow, glossary, memory, templates/, etc). Seeded from `framework/templates/CONTEXT.md.example`.
7. `company/philosophy.md` — optional (skipped while empty; templates in `framework/templates/philosophies/`).
8. `## Team` (dynamic) — peers the agent can call via `ask_agent` (filtered by `agent_policies.can_ask`).

**PWA editor:** Settings -> Workflows edits `steps.<step>.instructions` directly (textarea with a `|-` yaml block scalar on save). Settings -> Preview simulates context via params (`?mode=child&parent=<x>&task_slug=<y>`) to see the final concatenation for any agent in any scenario.

**Backend mirror:** `framework/web/app/main.py:_preview_*_block` reproduces the runner's logic for the preview. If you change one of the two, sync the other.

**Hard rule:** framework mechanics (broker, hierarchy, MCP tools) stay in `platform.md`; phase mechanics in `workflows.yaml.steps.<step>.instructions` (instance, but per workflow, not per agent); role identity + stack in `agents/<name>/CLAUDE.md`. If an agent's CLAUDE.md starts talking about `complete_phase`/`create_worktree`/`ask_human` in a child/etc, something has moved back into the wrong layer.

## Keeping `.dev/notes/` in sync

If you're hacking on the framework and have a `.dev/notes/` (PROJECT_PLAN, EXECUTION_LOG, QUESTIONS, DECISIONS), keep it in sync — notes are only useful if they're up to date. History shows it's easy to forget during bursts of commits, and a future Claude coming in "cold" needs these files as a map.

- **After EVERY commit or cluster that closes a feature:**
  - Update the 1st line of [.dev/notes/EXECUTION_LOG.md](.dev/notes/EXECUTION_LOG.md) if the current state changed
  - Add 1 new entry at the top of EXECUTION_LOG describing what changed
  - Autonomous architectural decision → record it in [.dev/notes/DECISIONS.md](.dev/notes/DECISIONS.md) with a new **D-NN**
  - Question resolved in QUESTIONS.md? Move/remove it.
- **When starting a new session:** `git log --oneline` since the last notes update (`stat -c "%y" .dev/notes/EXECUTION_LOG.md`). Gap > 3 commits without an update → **propose a catch-up before starting a new feature**.
- **Before a big feature:** is PROJECT_PLAN.md consistent with the current architecture? If not, refresh it first. Commits without notes = work without a map for a future Claude.
- **"Sufficient update" criterion:** a new Claude reading the 4 files can (a) describe today's state in 3 sentences, (b) know why each recent decision was made, (c) know what is open. If not, an update is missing.

This rule is an **operational invariant** — same weight as "run the tests before committing". When in doubt whether it's worth updating, **it is**.

## Quick access

- **`./run.sh` (main entry point):** `./run.sh help` lists everything — `install`, `setup`, `submodules [--latest]`, `reconcile`, `start`/`stop`/`restart`, `build`, `rebuild <svc>`, `status`, `logs [svc|agent]`, `shell <agent>`, `psql`, `test [unit|services|e2e|all]`, `smoke`, `backup`, `reset`. The Makefile targets still work.
- **Web PWA (primary interface):** `http://localhost:9090` — conversations, voice→text recording, file viewer/upload, screenshot paste, VAPID push, telemetry 📊 (cost/tokens/latency), memory 🧠, hire a new agent 👔.
- **Bootstrap from scratch:** `./run.sh install` (or `bash framework/scripts/bootstrap-env.sh && make reconcile` after `make submodules`). The bootstrap creates `.env` with random secrets + pre-creates `instance/{agents,company,repos,worktrees,backups,sessions,heartbeats}` + copies `agents.yaml.example`. Reconcile detects postgres+web not being up and starts them automatically. The scheduler is DB-backed (D-112): custom jobs created via PWA `/scheduler`, native overrides via `/settings/routines`.
- **Check containers:** `docker compose ps` or `./run.sh status` (at least 7: postgres, web, tts, transcriber, watchdog, orchestrator-reactor, scheduler + N `agent-<name>` services).
- **Structured JSON logs:** `./run.sh logs <agent>`, `make logs-agent-<agent>` or `docker compose logs -f agent-<agent>`.
- **Shell in an agent:** `./run.sh shell <agent>` or `make shell-agent-<agent>`.
- **Tests:** `./run.sh test` (agent + web unit tests in `.venv`), `./run.sh test services` (ai-tts + ai-transcriber suites), `./run.sh test e2e` (Playwright).
- **Task status:** `find instance/company/tasks/ -name "metadata.yaml" -exec head -5 {} \;`.
- **Pending events:** query Postgres — `docker compose exec postgres psql -U ai_company -d ai_company -c "SELECT id, event_type, status FROM orchestrator.events WHERE status='pending';"` (or `./run.sh psql -c "..."`).
- **New agent:** edit `instance/agents/agents.yaml` (or `make new-agent NAME=<slug> DISPLAY="Name"`), then `make reconcile` (idempotent — handles user+stream in the broker, .env, override, compose up). Tools: list only what the agent needs on top of `allowed_tools_defaults`, using `group:<name>` (built-ins in `BUILTIN_TOOL_GROUPS`, [framework/scripts/reconcile.py](framework/scripts/reconcile.py)); reconcile warns on pinned `claude-*` model ids and on framework mechanics in an agent's `CLAUDE.md`.
- **New repo for agents:** agents call the `init_repo` MCP tool (web creates it via `POST /api/repos/init`); after adding repos by hand, `make reconcile` so agents get the per-repo `.git/` mounts.
- **Transcribe audio:** automatic in the PWA (click the record button). Ad-hoc: `curl -X POST http://transcriber:8000/transcribe -F file=@audio.mp3` from inside the compose network (ai-transcriber's native API; add `-H "Authorization: Bearer $TRANSCRIBER_API_KEY"` if set).
- **Synthesize speech:** `POST http://tts:8000/synthesize` with `{"text": "...", "format": "wav"|"mp3"}` inside the compose network (ai-tts); the PWA goes through `/api/tts/synthesize`. `./run.sh smoke` exercises both services.
- **Auto-recovery:** claude_runner retries once on SIGKILL/SIGTERM with `--resume`. The watchdog monitors heartbeats in `instance/heartbeats/` and restarts agents stale > 180s (cooldown 10min). `DRY_RUN=1` on the watchdog for observer mode.
- **ask_agent (MCP tool):** agent A consults agent B via `ask_agent(target_agent, question)`. Topic `__ask-from-A-<uid>` in `#B`. Default timeout 30min, extended indefinitely if B is blocked on `ask_human`. `__*` topics are hidden in the PWA except when they have a `pending_ask`. Enable it by adding `mcp__ai_company__ask_agent` to `allowed_tools`.
- **Database directly:** `./run.sh psql` or `docker compose exec postgres psql -U ai_company -d ai_company`. Schemas: `messaging`, `orchestrator`, `memory`, `telemetry`, `web`.

## Working on the PWA / frontend

- **Before touching layout/UI:** read [framework/web/frontend/MOBILE.md](framework/web/frontend/MOBILE.md). It collects real pitfalls (`flex-wrap` + `truncate`, `shrink-0` on button groups, `pb-safe-nav` vs `pb-bottomNav`, bundle cache). Reviewing it costs less than hunting horizontal overflow on a physical device.
- **Cache (D-96):** `index.html`, `sw.js`, `manifest.webmanifest` are served with `Cache-Control: no-cache`; `/_app/*` (hashed bundles) with `max-age=31536000, immutable`. Don't remove these headers — without them, a rebuild doesn't show up for the user.
- **Validate mobile via Playwright:** viewport 390x844 (iPhone 14). Quick dump:
  ```js
  await page.setViewportSize({ width: 390, height: 844 });
  page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  // > 0 = the page has horizontal overflow.
  ```
