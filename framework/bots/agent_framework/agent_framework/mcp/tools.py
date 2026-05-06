"""Definicoes de tools MCP expostas pelo framework.

Contrato: toda tool retorna texto no campo `content[0].text` do JSON-RPC
response. Onde a acao tem consequencia de protocolo (complete_phase,
register_worktree), o texto tambem carrega uma **guidance** curta com o
que aconteceu e qual o proximo passo esperado. Agentes leem essa guidance
e nao precisam inferir o protocolo.
"""
from __future__ import annotations

from typing import Any


ASK_HUMAN_TOOL: dict[str, Any] = {
    "name": "ask_human",
    "description": (
        "Ask the human orchestrator a question AND BLOCK until they answer. "
        "Use for decisions that change scope/architecture, ambiguities that "
        "affect direction, or tradeoffs with no objectively better answer.\n\n"
        "Blocks indefinitely until the human responds — there is no timeout. "
        "If your work cannot wait for a human and there is no objectively "
        "right answer, use `complete_phase(next='halt')` instead — it "
        "pauses the task cleanly and the human resumes when ready.\n\n"
        "DO NOT use for:\n"
        "  - Plain notifications (use `notify_human`).\n"
        "  - Handoffs between agents (use `complete_phase`).\n"
        "  - Questions you can answer yourself or via file reads."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question for the human. Specific, self-contained.",
            },
            "context": {
                "type": "string",
                "description": "Background the human needs (decisions tried, constraints, tradeoffs).",
                "default": "",
            },
        },
        "required": ["question"],
    },
}


NOTIFY_HUMAN_TOOL: dict[str, Any] = {
    "name": "notify_human",
    "description": (
        "Post a real chat message to the human in the current topic — WITHOUT "
        "waiting for a response and WITHOUT creating a pending_ask. The "
        "message persists as a bubble in the conversation history.\n\n"
        "WHEN TO USE:\n"
        "- Mid-turn status that should remain in chat history (e.g., "
        "'starting execution of <slug>' before a long sequence of tool "
        "calls). Without this, only thinking events stream while the turn "
        "runs — they disappear from history once the turn ends.\n"
        "- Posting status after a turn-finalizing tool like `complete_phase` "
        "(which ends the turn without you producing a final reply). Without "
        "`notify_human` the human only sees the reactor's terminal "
        "notification, not your own framing of what happened.\n\n"
        "WHEN NOT TO USE:\n"
        "- As a substitute for the final reply text. Just respond normally; "
        "the last text block of your turn already becomes a chat bubble.\n"
        "- For 'thinking out loud' mid-turn. Plain text between tool calls "
        "already streams as live thinking events (visible while the turn "
        "runs, though they don't persist as bubbles after it ends).\n\n"
        "If you want the human to actually answer, use `ask_human` instead "
        "(blocks the run until they respond)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "Free-form text. Markdown supported.",
            },
        },
        "required": ["message"],
    },
}


ARCHIVE_CONVERSATION_TOOL: dict[str, Any] = {
    "name": "archive_conversation",
    "description": (
        "Archive the current conversation. The thread is moved to the human's "
        "Closed tab in the PWA, runners are cancelled, and direct descendants "
        "(ask_agent sub-conversations, task children) are archived along.\n\n"
        "Designed for one-shot scheduled jobs and notifications that don't "
        "deserve permanent sidebar real estate. Typical usage: a daily "
        "reminder posts content, you respond, then call this tool to keep "
        "the human's sidebar clean.\n\n"
        "DO NOT call this:\n"
        "  - When you have an open `ask_human` (you would silence the human "
        "    before they reply — the tool refuses).\n"
        "  - Mid-task — archiving an active task's origin conversation hides "
        "    progress from the human. Finish the work first.\n\n"
        "Reversible: the human can unarchive from the Closed tab."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {},
    },
}


COMPLETE_PHASE_TOOL: dict[str, Any] = {
    "name": "complete_phase",
    "description": (
        "Mark the current step of a multi-agent task as done and hand off "
        "to the next step (or terminate the task). You MUST have written "
        "the `artifact` file before calling this tool — the tool verifies "
        "it exists.\n\n"
        "Step names and valid transitions are declared by the instance in "
        "`company/workflows.yaml` — the tool validates your `next` value "
        "against that definition. Error messages tell you which transitions "
        "are allowed from your current step.\n\n"
        "Protocol terminals (these names are fixed by the framework, not the "
        "instance; each maps to a task status):\n"
        "  `done`         — task completed successfully (status=done).\n"
        "  `halt`         — pause; human must unblock  (status=blocked).\n"
        "  `human_review` — escalate; human decides    (status=human_review).\n\n"
        "The framework:\n"
        "  1. Persists task state to the database (schema `tasks`: tasks, phases, worktrees). "
        "You never touch task state by hand — always go through this tool.\n"
        "  2. Emits an orchestrator event — the next agent receives the handoff automatically.\n"
        "  3. Returns a `guidance` string describing what just happened and what, if anything, "
        "     is still up to you.\n\n"
        "DO NOT post an extra message in the conversation after calling this tool just to "
        "announce 'despachado' — the handoff is already automatic. If you want to surface a "
        "status to the human, use `notify_human` instead."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "task_slug": {
                "type": "string",
                "description": "Kebab-case slug — matches folder `company/tasks/<slug>/`.",
            },
            "artifact": {
                "type": "string",
                "description": (
                    "Filename of the artifact you wrote in this phase, relative to the task folder."
                ),
            },
            "summary": {
                "type": "string",
                "description": "1-3 lines summarizing the outcome of this phase.",
            },
            "next": {
                "type": "string",
                "description": (
                    "Next step name (as declared in `company/workflows.yaml`) OR a terminal: "
                    "`done`, `halt`, `human_review`."
                ),
            },
            "next_agent": {
                "type": "string",
                "description": (
                    "Optional override of the default agent for the next step. REQUIRED "
                    "when the next step has no `agent` default in `workflows.yaml` (typical "
                    "of steps that fan out to multiple variants — e.g. a step with several "
                    "concrete agents the caller must pick one from)."
                ),
            },
            "next_topic": {
                "type": "string",
                "description": (
                    "Optional topic override for the next agent's thread. Default resolution: "
                    "when the handoff returns to the task's origin agent (the one that opened "
                    "the task), the reactor posts in the origin conversation so the human sees "
                    "it inline; otherwise it uses `task-<slug>`. Pass an explicit value to force "
                    "a specific topic. Keep short (<60 chars) — it shows in the sidebar."
                ),
            },
            "title": {
                "type": "string",
                "description": (
                    "Optional task title. Only takes effect on the FIRST call (when the "
                    "task is created). Ignored on subsequent calls."
                ),
            },
            "workflow": {
                "type": "string",
                "description": (
                    "Optional workflow name (from company/workflows.yaml). Only takes "
                    "effect on the FIRST call."
                ),
            },
            "complexity": {
                "type": "string",
                "description": "Free-form complexity (e.g. pequeno/medio/grande). First-call only.",
            },
            "impact": {
                "type": "string",
                "description": "Free-form impact (e.g. alto/medio/baixo). First-call only.",
            },
            "difficulty": {
                "type": "string",
                "description": "Free-form difficulty. First-call only.",
            },
            "origin": {
                "type": "string",
                "description": "Free-form origin note (human description). First-call only.",
            },
            "baseline": {
                "type": "object",
                "description": (
                    "Optional dict mapping `repo -> commit_sha`. Merged into the task's "
                    "metadata_extra.baseline. Additive: new repos are added; existing "
                    "SHAs are NOT overwritten (baseline is immutable per repo). Use when "
                    "pinning baseline at the end of the analysis phase so the executor "
                    "creates worktrees against a fixed point."
                ),
                "additionalProperties": {"type": "string"},
            },
            "standalone": {
                "type": "boolean",
                "description": (
                    "Optional. Default false. When true, the new conversation created "
                    "by this handoff is NOT linked as a child of the origin conversation "
                    "(parent_conv_id is left null). Use this for fan-out from a chat into "
                    "multiple independent task conversations: each task conv shows up as "
                    "a top-level entry in the sidebar and is conversable by the human, "
                    "instead of becoming a read-only chip under the origin chat (which is "
                    "the default per D-96 — fits multi-agent flows where the orchestrator "
                    "is the only conv the human talks to). Logical link task<->origin is "
                    "still preserved via `tasks.origin_stream/origin_topic` in DB. Has no "
                    "effect when the destination conversation already exists (handoff "
                    "between phases of the same task) — parent_conv_id is set only on "
                    "creation."
                ),
            },
        },
        "required": ["task_slug", "artifact", "summary", "next"],
    },
}


CREATE_WORKTREE_TOOL: dict[str, Any] = {
    "name": "create_worktree",
    "description": (
        "Create an isolated git worktree for the task and register it in the "
        "database (`tasks.worktrees`) atomically. Worktrees live at "
        "`/workspace/worktrees/<repo>/<slug>/` — out of the canonical repo "
        "tree, so they never pollute `repos/<repo>/` status.\n\n"
        "Call this as your FIRST action in the execution phase, before editing "
        "anything. The tool:\n"
        "  1. Resolves `baseline_sha` if not provided (default-branch HEAD of "
        "the remote — works regardless of whether the repo uses `main` or "
        "`master`).\n"
        "  2. Runs `git worktree add -b <branch> <path> <sha>` in "
        "`/workspace/repos/<repo>`.\n"
        "  3. Upserts into `tasks.worktrees` + writes `baseline.<repo>` to "
        "task metadata.\n\n"
        "Idempotent: re-invoking for the same (task, repo, branch) returns the "
        "existing worktree if path + HEAD are consistent; otherwise recreates.\n\n"
        "After success, `cd` into the returned `path` and work from there — "
        "never edit `/workspace/repos/<repo>/` directly."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "task_slug": {
                "type": "string",
                "description": "Kebab-case slug of the task.",
            },
            "repo": {
                "type": "string",
                "description": (
                    "Short repo key — same key used in `metadata.baseline.<repo>`. "
                    "Must match a directory under `/workspace/repos/`."
                ),
            },
            "baseline_sha": {
                "type": "string",
                "description": (
                    "Optional. SHA to use as worktree base. If omitted, the "
                    "server resolves it: `git fetch --quiet` + the commit that "
                    "`origin/HEAD` points to. Provide explicitly when planning "
                    "froze a specific baseline (workflow `completo`: tech-lead "
                    "passes it via `complete_phase(baseline={...})`)."
                ),
            },
            "branch": {
                "type": "string",
                "description": (
                    "Optional. Branch name for the worktree. Defaults to "
                    "`task/<task_slug>`."
                ),
            },
        },
        "required": ["task_slug", "repo"],
    },
}


CLEANUP_WORKTREES_TOOL: dict[str, Any] = {
    "name": "cleanup_worktrees",
    "description": (
        "Remove all worktrees registered for a task. Runs "
        "`git worktree remove --force` + `git worktree prune` in each "
        "canonical repo, then deletes rows from `tasks.worktrees`. "
        "Idempotent — calling twice on a clean task is a no-op.\n\n"
        "Intended use: call this at task wrap-up, AFTER the MR/PR is open "
        "on the remote (the branch + diff live in the remote now; the "
        "local worktree has served its purpose). Typical caller is the "
        "agent that finalizes the task.\n\n"
        "Returns `removed` (list of successfully cleaned worktrees) and "
        "`failed` (list with error detail per failure). If `failed` is "
        "non-empty, DO NOT proceed to task-terminal transitions — escalate "
        "to human review instead. Failures are not masked."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "task_slug": {
                "type": "string",
                "description": "Kebab-case slug of the task.",
            },
        },
        "required": ["task_slug"],
    },
}


GET_TASK_STATE_TOOL: dict[str, Any] = {
    "name": "get_task_state",
    "description": (
        "Read structured task state. Returns: slug, title, workflow, status, "
        "current_step, current_agent, complexity, baseline (per repo), worktrees, "
        "origin (stream/topic), phases_done, blocked_reason. Always use this — "
        "never try to parse task state from files on disk."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "task_slug": {"type": "string", "description": "Kebab-case slug."},
        },
        "required": ["task_slug"],
    },
}


ASK_AGENT_TOOL: dict[str, Any] = {
    "name": "ask_agent",
    "description": (
        "Pause execution and ask ANOTHER agent a question. Use when the answer "
        "requires expertise of another agent AND waiting is cheaper than a full "
        "task handoff (via `complete_phase`). Example shape: a caller asks a "
        "planner agent about implementation ordering before committing to an "
        "approach.\n\n"
        "Blocks until the target agent responds or until timeout. If the target "
        "itself blocks on ask_human, this call auto-extends indefinitely until "
        "human unblocks the chain.\n\n"
        "A new topic `__ask-from-<your-agent>-<uid>` is created in the target's "
        "stream to carry the side-conversation.\n\n"
        "DO NOT use for questions you can answer via file reads or memory."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "target_agent": {
                "type": "string",
                "description": "Slug of the target agent — must exist in agents.yaml.",
            },
            "question": {
                "type": "string",
                "description": "The question. Specific and self-contained.",
            },
            "context": {
                "type": "string",
                "description": "Background the target agent needs to answer well.",
                "default": "",
            },
            "timeout_minutes": {
                "type": "number",
                "description": (
                    "Give up waiting after N minutes IF target is NOT blocked on "
                    "human. Auto-extends while target has a pending ask_human. "
                    "Default 30."
                ),
                "default": 30,
            },
        },
        "required": ["target_agent", "question"],
    },
}


ASK_AGENTS_MANY_TOOL: dict[str, Any] = {
    "name": "ask_agents_many",
    "description": (
        "Run MULTIPLE asks/dispatches IN PARALLEL in a single tool call. Two "
        "main use cases:\n"
        "  1. Parallel consultation — N independent queries (e.g. asking "
        "     database-engineer AND executor-legado the same task at once).\n"
        "  2. Parallel dispatch of work — when an orchestrator (e.g. "
        "     product-owner) needs to delegate independent units of work to "
        "     N agents whose outputs do NOT depend on each other (e.g. an "
        "     execution plan marking executor-ts and executor-core as "
        "     parallel — each implements its slice without waiting for the "
        "     other). Each target replies with its result; you collect all "
        "     and proceed.\n\n"
        "Behaves like running N `ask_agent` calls concurrently — but the MCP "
        "transport serializes individual tool calls, so issuing `ask_agent` "
        "twice back-to-back runs them sequentially. This tool sidesteps that "
        "by running all calls server-side via asyncio.gather.\n\n"
        "**Use this whenever you would otherwise call `ask_agent` >=2 times "
        "in a row** — for both consultation and dispatch.\n\n"
        "Blocks until ALL asks resolve or time out. Per-ask timeout auto-extends "
        "if a target is blocked on ask_human (same as ask_agent). Per-ask "
        "policy/cycle/depth checks apply; a single ask failing does NOT fail the "
        "batch — the failure is reported inline in the response.\n\n"
        "Response is a single text with each target's answer (or error) clearly "
        "labeled. Use `ask_agent` for a single target; use this only for >=2.\n\n"
        "DO NOT use for questions you can answer via file reads or memory."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "asks": {
                "type": "array",
                "minItems": 2,
                "maxItems": 10,
                "description": (
                    "List of questions to dispatch in parallel. Each item targets "
                    "a different agent (or the same agent with different questions)."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "target_agent": {
                            "type": "string",
                            "description": "Slug of the target agent — must exist in agents.yaml.",
                        },
                        "question": {
                            "type": "string",
                            "description": "The question. Specific and self-contained.",
                        },
                        "context": {
                            "type": "string",
                            "description": "Background the target agent needs to answer well.",
                            "default": "",
                        },
                    },
                    "required": ["target_agent", "question"],
                },
            },
            "timeout_minutes": {
                "type": "number",
                "description": (
                    "Give up waiting on each ask after N minutes IF target is NOT "
                    "blocked on human. Auto-extends per-ask while target has a "
                    "pending ask_human. Default 30. Applies independently to each ask."
                ),
                "default": 30,
            },
        },
        "required": ["asks"],
    },
}


MEMORY_SAVE_TOOL: dict[str, Any] = {
    "name": "memory_save",
    "description": (
        "Save a long-term fact to this agent's memory. For information you want "
        "to recall in future sessions: user preferences, decisions made, "
        "conventions, project state. DO NOT use for ephemeral data (work-in-"
        "progress notes belong in files). Upsert: saving the same key replaces "
        "the value. Keys: snake_case or dot.notation, canonical (ex: "
        "'user.preferred_language', 'proj.deploy_target')."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Canonical identifier."},
            "value": {"type": "string", "description": "Content to remember."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional labels (ex: ['user-pref', 'deploy']).",
                "default": [],
            },
        },
        "required": ["key", "value"],
    },
}


MEMORY_RECALL_TOOL: dict[str, Any] = {
    "name": "memory_recall",
    "description": (
        "Search this agent's long-term memory. Returns up to `limit` facts ordered "
        "by relevance (full-text) or recency. Use at the start of a task to check "
        "if you already know relevant context."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Free-text query."},
            "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 50},
        },
        "required": ["query"],
    },
}


MEMORY_LIST_TOOL: dict[str, Any] = {
    "name": "memory_list",
    "description": (
        "List recent facts in this agent's memory (no query). Useful to audit "
        "what you know or browse by tag."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
            "tag": {"type": "string", "description": "Filter by specific tag."},
        },
    },
}


MEMORY_EDIT_TOOL: dict[str, Any] = {
    "name": "memory_edit",
    "description": (
        "Update an existing fact. Fails if the key does not exist (use memory_save "
        "to create). Provide `value` and/or `tags` to replace. Omitted fields are "
        "preserved."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Canonical identifier."},
            "value": {"type": "string", "description": "New content. Omit to keep current."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "New tag list (replaces existing). Omit to keep current.",
            },
        },
        "required": ["key"],
    },
}


MEMORY_DELETE_TOOL: dict[str, Any] = {
    "name": "memory_delete",
    "description": (
        "Permanently remove a fact. Hard delete — there is no undo."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Canonical identifier."},
        },
        "required": ["key"],
    },
}


BACKLOG_ADD_TOOL: dict[str, Any] = {
    "name": "backlog_add",
    "description": (
        "Register an idea/pending item in the company backlog (shared across "
        "agents and visible in the PWA). Use for work the human/team should "
        "eventually triage — NOT for immediate tasks (those go through the "
        "workflow via complete_phase).\n\n"
        "Priority convention: -2 (low) ... 0 (normal) ... +2 (critical). "
        "Items default to status=aberto (triaged, awaiting prioritization). "
        "Pass status='rascunho' for raw brain-dump capture when the human "
        "wants the idea recorded without an interview — skip classification "
        "(leave impact/effort unset, priority=0), set content to the verbatim "
        "request, and confirm in one line. Drafts stay hidden from the default "
        "backlog list; they surface via the daily curation query "
        "(status='rascunho' AND age>7d) or explicit filter.\n\n"
        "The human or another agent can later promote an item to a task via "
        "`backlog_promote` (creates a task; item stays as linked history)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "description": "Kebab-case identifier unique within the instance.",
            },
            "title": {"type": "string", "description": "Short one-line title."},
            "content": {
                "type": "string",
                "description": "Markdown body (motivation, context, criteria).",
                "default": "",
            },
            "priority": {
                "type": "integer",
                "description": "-2 (low) to +2 (critical). Default 0 (normal).",
                "default": 0,
            },
            "impact": {
                "type": "string",
                "description": "Free-form (e.g. 'alto', 'medio', 'baixo').",
            },
            "effort": {
                "type": "string",
                "description": "Free-form effort estimate (same scale as impact).",
            },
            "status": {
                "type": "string",
                "description": (
                    "Initial status. Default 'aberto' (triaged). Pass "
                    "'rascunho' for raw capture awaiting specification. "
                    "Other values ('em_execucao', 'promovido', 'concluido', "
                    "'descartado') are valid but unusual at creation time."
                ),
                "default": "aberto",
            },
        },
        "required": ["slug", "title"],
    },
}


BACKLOG_LIST_TOOL: dict[str, Any] = {
    "name": "backlog_list",
    "description": (
        "List backlog items ordered by priority desc (critical first), then "
        "updated_at desc. Default filters status='aberto' — drafts "
        "(status='rascunho') are NOT included in the default list; pass "
        "status='rascunho' to surface raw captures pending specification, "
        "or status='all' for everything. Use to surface items that still "
        "need triage. Returns: slug, title, priority, impact, effort, "
        "status, promoted_task_slug, created_by, updated_at."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "Filter by status. Pass 'all' to list everything.",
                "default": "aberto",
            },
            "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
        },
    },
}


BACKLOG_UPDATE_TOOL: dict[str, Any] = {
    "name": "backlog_update",
    "description": (
        "Update fields of an existing backlog item. Omitted fields stay "
        "unchanged. Use to re-prioritize, refine the description, or change "
        "impact/effort. To close without promoting to a task, set "
        "status='descartado'."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "slug": {"type": "string"},
            "title": {"type": "string"},
            "content": {"type": "string"},
            "priority": {"type": "integer"},
            "impact": {"type": "string"},
            "effort": {"type": "string"},
            "status": {
                "type": "string",
                "description": (
                    "aberto | rascunho | em_execucao | promovido | concluido "
                    "| descartado. Common transitions: 'rascunho' → 'aberto' "
                    "when a draft gets specified; any → 'descartado' to "
                    "archive without promoting. 'concluido' is set "
                    "automatically when the linked task hits terminal done."
                ),
            },
        },
        "required": ["slug"],
    },
}


BACKLOG_PROMOTE_TOOL: dict[str, Any] = {
    "name": "backlog_promote",
    "description": (
        "Promote a backlog item to an active task. Creates a new task with "
        "the given slug (defaults to the backlog slug if not provided), "
        "copies title/content, and marks the backlog item as status=promovido "
        "linked via promoted_task_slug.\n\n"
        "The task is created in 'in_progress' status with no phases yet — "
        "use `complete_phase` on the new task to advance it through the "
        "workflow. Typically called by the human operator via PWA 'executar "
        "agora' button, but agents can call it too."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "Backlog item slug."},
            "task_slug": {
                "type": "string",
                "description": "Optional task slug (defaults to backlog slug).",
            },
            "workflow": {
                "type": "string",
                "description": "Workflow name (from company/workflows.yaml).",
            },
            "next_agent": {
                "type": "string",
                "description": (
                    "Agent to dispatch the task to first. Required if the "
                    "workflow's initial step has no default agent."
                ),
            },
            "initial_topic": {
                "type": "string",
                "description": "Topic for the first handoff. Default: task-<slug>.",
            },
        },
        "required": ["slug"],
    },
}


TASK_LIST_TOOL: dict[str, Any] = {
    "name": "task_list",
    "description": (
        "List active tasks (default) or include archived. Ordered by "
        "updated_at desc. Returns: slug, title, status, current_step, "
        "current_agent, workflow, updated_at, phases_count, archived. Use "
        "for cross-task coordination — eg. when an agent needs to check "
        "ongoing work before starting something new."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "include_archived": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
        },
    },
}


REOPEN_TASK_TOOL: dict[str, Any] = {
    "name": "reopen_task",
    "description": (
        "Reopen a task that is in a terminal state (done, blocked, human_review) "
        "and dispatch it to run another step. Use ONLY when the human explicitly "
        "asks for a task to be resumed after you closed it — do NOT call this "
        "proactively; a new task is usually the right answer.\n\n"
        "Effect: task status goes back to 'in_progress', current_step/current_agent "
        "are set to the requested next_step/next_agent, blocked_reason is cleared, "
        "and the reactor dispatches a phase_complete event to the target agent. "
        "The reason string is persisted in the event payload for audit."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "task_slug": {"type": "string"},
            "next_step": {
                "type": "string",
                "description": (
                    "Step name (from workflows.yaml) the task should resume at. "
                    "Terminal values (done/halt/human_review) are rejected."
                ),
            },
            "next_agent": {
                "type": "string",
                "description": (
                    "Agent to handle the reopened step. Required if the step has "
                    "no default agent in the workflow."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "Why the human wants to reopen. Free-form but required; kept "
                    "in the event payload for traceability."
                ),
            },
        },
        "required": ["task_slug", "next_step", "reason"],
    },
}


SCHEDULE_ADD_TOOL: dict[str, Any] = {
    "name": "schedule_add",
    "description": (
        "Create a new recurring scheduled job that runs at the specified "
        "cron expression. Agents can ONLY schedule `action=post_message` "
        "(send a message to a stream/topic on a recurring basis). For other "
        "action types (backups, cleanups, cost checks) ask the human via "
        "ask_human to configure them in the PWA Settings -> Routines page.\n\n"
        "Cron syntax: 5-field crontab (`min hour dom month dow`). Examples:\n"
        "  '0 9 * * MON-FRI'  — every weekday at 09:00\n"
        "  '*/30 * * * *'     — every 30 minutes\n"
        "  '0 18 * * *'       — daily at 18:00\n\n"
        "Jobs persist across scheduler restarts (stored in the DB) and are "
        "hot-reloaded — no container restart needed. Visible in the PWA at "
        "/scheduler where any agent or human can edit/delete."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "description": "Kebab-case unique identifier (e.g. 'daily-status-9am').",
            },
            "cron": {
                "type": "string",
                "description": "5-field cron expression.",
            },
            "action": {
                "type": "string",
                "description": "Must be 'post_message' when called by an agent.",
                "enum": ["post_message"],
            },
            "params": {
                "type": "object",
                "description": (
                    "Action-specific params. For post_message: "
                    "{stream, topic, content}."
                ),
            },
            "description": {
                "type": "string",
                "description": "Optional human-readable note about why this job exists.",
            },
        },
        "required": ["slug", "cron", "action", "params"],
    },
}


SCHEDULE_LIST_TOOL: dict[str, Any] = {
    "name": "schedule_list",
    "description": (
        "List all custom scheduled jobs (created via MCP or PWA). Does NOT "
        "include native platform jobs (backups/cleanups/cost checks) — those "
        "live in the framework defaults and are managed in Settings -> Routines."
    ),
    "inputSchema": {"type": "object", "properties": {}},
}


SCHEDULE_UPDATE_TOOL: dict[str, Any] = {
    "name": "schedule_update",
    "description": (
        "Edit an existing custom scheduled job. Any omitted field is kept. "
        "Cannot change `action` via this tool — if you need a different "
        "action, remove + recreate. Hot-reloaded: the scheduler picks up the "
        "change without restart."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "slug": {"type": "string"},
            "cron": {"type": "string", "description": "New cron expression."},
            "params": {
                "type": "object",
                "description": "New action params (replaces the previous dict).",
            },
            "description": {"type": "string"},
            "enabled": {
                "type": "boolean",
                "description": "Toggle the job on/off without deleting it.",
            },
        },
        "required": ["slug"],
    },
}


SCHEDULE_REMOVE_TOOL: dict[str, Any] = {
    "name": "schedule_remove",
    "description": (
        "Permanently delete a custom scheduled job. The scheduler unregisters "
        "it from runtime immediately. Only affects custom jobs — native "
        "routines cannot be deleted (disable them via Settings -> Routines)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"slug": {"type": "string"}},
        "required": ["slug"],
    },
}


# ---------- Skills (per-agent, persistent across sessions) ----------
#
# Claude Code CLI hardcodes a write-block on `~/.claude/` even under
# `--dangerously-skip-permissions`. To let agents accumulate their own
# skills, we expose these MCP tools — they write to a path the CLI is
# allowed to touch (`/app/agents/<name>/skills/`), and the entrypoint
# symlinks `~/.claude/skills` to it. Skills written this turn become
# available on the NEXT spawn (CLI reads skills at startup, no in-session
# reload).

SAVE_SKILL_TOOL: dict[str, Any] = {
    "name": "save_skill",
    "description": (
        "Persist a Claude Code skill in this agent's own skill library. "
        "Use to package a reusable procedure you want available in future "
        "sessions (your own — skills are NOT shared across agents). The "
        "skill becomes available on the NEXT turn (CLI loads skills at "
        "startup, not mid-session). Upsert: saving the same name replaces "
        "the previous SKILL.md."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Slug: lowercase, alphanumeric + hyphens, max 50 chars. "
                    "Becomes the directory name. Example: 'lint-php-file'."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "One-line description shown when the skill is matched. "
                    "Max 500 chars. Should make it obvious WHEN to use the "
                    "skill (e.g. 'Lint a single PHP file via the project's "
                    "configured phpcs ruleset')."
                ),
            },
            "body": {
                "type": "string",
                "description": (
                    "SKILL.md body in markdown (without the frontmatter — "
                    "name/description are added automatically). Max 100KB. "
                    "Document the procedure, inputs, edge cases, and any "
                    "shell commands the skill expects you to run."
                ),
            },
        },
        "required": ["name", "description", "body"],
    },
}


LIST_SKILLS_TOOL: dict[str, Any] = {
    "name": "list_skills",
    "description": (
        "List skills currently saved in this agent's library. Returns name + "
        "description for each. Use to audit your library or check whether a "
        "skill already exists before creating a new one."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {},
    },
}


DELETE_SKILL_TOOL: dict[str, Any] = {
    "name": "delete_skill",
    "description": (
        "Permanently remove a skill from this agent's library. Hard delete — "
        "the SKILL.md and its directory are removed. Takes effect on the "
        "next turn."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Slug of the skill to remove.",
            },
        },
        "required": ["name"],
    },
}


ALL_TOOLS: list[dict[str, Any]] = [
    ASK_HUMAN_TOOL,
    NOTIFY_HUMAN_TOOL,
    ARCHIVE_CONVERSATION_TOOL,
    COMPLETE_PHASE_TOOL,
    CREATE_WORKTREE_TOOL,
    CLEANUP_WORKTREES_TOOL,
    GET_TASK_STATE_TOOL,
    ASK_AGENT_TOOL,
    ASK_AGENTS_MANY_TOOL,
    MEMORY_SAVE_TOOL,
    MEMORY_RECALL_TOOL,
    MEMORY_LIST_TOOL,
    MEMORY_EDIT_TOOL,
    MEMORY_DELETE_TOOL,
    BACKLOG_ADD_TOOL,
    BACKLOG_LIST_TOOL,
    BACKLOG_UPDATE_TOOL,
    BACKLOG_PROMOTE_TOOL,
    TASK_LIST_TOOL,
    REOPEN_TASK_TOOL,
    SCHEDULE_ADD_TOOL,
    SCHEDULE_LIST_TOOL,
    SCHEDULE_UPDATE_TOOL,
    SCHEDULE_REMOVE_TOOL,
    SAVE_SKILL_TOOL,
    LIST_SKILLS_TOOL,
    DELETE_SKILL_TOOL,
]
