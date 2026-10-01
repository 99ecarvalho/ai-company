## Platform rules (ai-company)

Operational honesty — MANDATORY:
- If a tool failed, was blocked by permissions, or doesn't exist, say so EXPLICITLY to the human. Never invent a successful result, and never describe outcomes you did not produce.
- Clearly distinguish: (a) what YOU did in this response using tools, (b) what you READ from a file, (c) what you INFERRED. When in doubt, read and quote; do not invent attribution.
- If a file has a completion marker (`status: done`, "completed", etc.) and you weren't the one who marked it, do NOT claim authorship. Describe what's written without assuming who wrote it.

Root -> child hierarchy (1 level; no grand-children):
- When you are invoked via `ask_agent` / `ask_agents_many` by another agent, you are operating in a **child conv** of theirs. The broker records this via `parent_conv_id` in Postgres (`messaging.conversations`).
- In a child conv, **`ask_human`, `ask_agent` and `ask_agents_many` are blocked** — the MCP gate rejects with 409 / InvalidParams. This is by design: the hierarchy is strictly root -> child, no deeper chains, to avoid deadlocks and noise.
- A child that needs external input (human decision, specialist consultation, opinion outside scope): **describe the request at the end of the response in a format ready for the parent to forward verbatim**, and end the turn. The parent receives the auto-reply, decides whether to answer directly, consult another agent, or escalate to the human, and calls you back via `ask_agent` in the same slot — the child conv receives a new turn via `--resume`.
- The system prompt, when assembled, includes a `## Invocation mode` block injected by the framework saying whether you are root or child (and who the parent is, if child). Use this block as the source of truth. In its absence (toggle off, fallback), assume child whenever the first message of the turn starts with "Question from `<agent>`" (the broker's standard prefix for `ask_agent`); otherwise, root.

Reply-as-gateway abstraction:
- When a role instruction says "call `ask_human` X" or "call `ask_agent <agent>` X" and you are in a child conv, **translate** to: "describe in the reply: needs human decision on X" / "describe in the reply: needs to consult <agent> on X".
- To escalate to the human: 2-4 lines in a format the parent can forward verbatim without reprocessing. No role jargon, do not transcribe the entire chain of reasoning.

Parallel work between agents — MANDATORY use of `ask_agents_many`:
- Whenever you need to invoke `ask_agent` against **2 or more** targets in sequence (parallel consultation OR independent work dispatch), use **one** call to `ask_agents_many` instead of N `ask_agent` calls. The MCP transport serializes individual tool calls — N `ask_agent` in sequence run one after another, even if you send them all in the same turn. `ask_agents_many` runs everything server-side via asyncio.gather (real parallelism).
- Classic case: the plan declares that two executors can work in parallel. Right form: 1 `ask_agents_many` with 2 asks (one per target). Wrong form: 2 `ask_agent` in sequence, OR `complete_phase` dispatching the second only after the first comes back.

Tools outside your allowed_tools:
- Your auto-approved tools are the ones listed in `.claude/settings.json` (the tools available to you). The system runs non-interactive — tools outside that list dead-end at a permission prompt.
- If you need a tool you don't have: tell the human "I don't have tool X enabled" and propose forwarding (`ask_agent` to an agent that has it) or an adjustment to `allowed_tools` in `agents.yaml`.

First contact with a task (phase handoff):
- Tasks with a workflow reach the agent via handoff: the previous agent calls `complete_phase(next=<step>, next_agent=<you>)` and the reactor posts a message in the `task-<slug>` conv on your stream. Your run's system prompt already comes with the `## Task state` block (DB snapshot) + `## Current phase instructions` block (markdown declared in `workflows.yaml`) — read those two before anything else; they define what you must produce and how to leave the step.
- To reopen an earlier step (revisit assumptions, redo plan after discovering a gap during execution), `complete_phase(next=<earlier-step>, summary=<reason>)` — the reactor routes back to the agent of that step. Use it when you need to rethink the approach; for a pinpoint question, prefer reply (child) or `ask_agent` (root).

Analysis mode (no task in progress):
- When the system prompt does **not** include the `## Task state` and `## Current phase instructions` blocks, you are not in a workflow phase — you are in analysis mode (ad-hoc question, code exploration, read-only consultation).
- In analysis mode: **do not call `create_worktree`**, **do not edit code**. Use `Read`/`Glob`/`Grep`/`Bash` (read-only git) to investigate, and return the diagnosis via auto-reply citing `<repo>/<path>:<line>` without transcribing large blocks.
- If the analysis becomes a candidate for execution, it's a fresh new flow: the parent opens a formal task with `complete_phase` and you receive the handoff with the contextual blocks.

Tasks, backlog and worktrees — always via MCP tools, never parse files:
- Task state: `get_task_state(task_slug=...)` returns slug, title, workflow, status, current_step, current_agent, complexity, baseline (per repo), worktrees, origin (stream/topic), phases_done, blocked_reason. When the framework injects the `## Task state` block in the system prompt, you already get this state up front — no need to call `get_task_state` to obtain what's already there; call it only when you need fresh data after a transition mid-turn.
- Advance/create phase: `complete_phase(task_slug, next, ...)`. On the first call of a new task, pass `title` and `workflow` (and optionally `complexity`/`impact`/`difficulty`/`origin`); the task is created and the extra fields are recorded in that call.
- Worktree: `create_worktree(task_slug, repo, baseline_sha=?, branch=?)` — creates the worktree at `/workspace/worktrees/<repo>/<slug>/` and registers it in the DB atomically. `baseline_sha` optional: when omitted, the server resolves it via the remote default branch HEAD. Available afterwards via `get_task_state`.
- **baseline_sha discipline**: in any execution step where the previous phase recorded a baseline (captured in the plan), **always** pass `baseline_sha=<state.baseline.<repo>>` obtained from `get_task_state` (or from the `## Task state` block). If the baseline is missing from metadata for some reason (wrong flow, previous phase didn't record), **do not guess** — return via `complete_phase(next="<earlier-step>", summary="baseline.<repo> missing")` registering the problem.
- Backlog: `backlog_add` / `backlog_list` / `backlog_update` / `backlog_promote`. If you don't have it in `allowed_tools`, forward via `ask_agent` to an agent that does.
- Artifacts (reports, scripts, diffs, markdown notes) are written via the `Write` tool to `company/tasks/<slug>/<file>`. The folder is created by the framework if it doesn't exist yet.

Persistent memory discipline:
- Memory is per agent (only you see your facts). Save with `memory_save(key=..., value=..., tags=[...])` (upsert by key; re-saving the same key overwrites it); find facts with `memory_recall(query=...)` or `memory_list()`. The most relevant ones are also injected into your prompt before each run (the `## Memory` block).
- **Test before saving: "memory != report".** Findings specific to the task (slug, IDs, monetary values, concrete dates, incident numbers) belong in `tasks/<slug>/` or in the diff/PR — **not in memory**. Memory captures reusable patterns: "I classified it as micro and it was medium — signals to watch"; "anti-bot at insurer X requires updated UC driver"; "human preferred X when the standard would have suggested Y".
- When you discover a memory is wrong, dated, or has been promoted to a permanent document, use `memory_edit` / `memory_delete` without remorse — dead memory misleads more than it helps.
- Role-specific discipline (examples of what's worth memorizing) lives in each agent's CLAUDE.md.

Skills discipline:
- Skills live at `./.claude/skills/<name>/SKILL.md` in the agent's directory (each agent sees its own).
- Create a skill when: a procedure was used **>=2 times successfully**; clear trigger (when to use) and clear non-application (when NOT to use); captures principle + abstract step. Hands-on skills, not theoretical essays.
- Role-specific candidates live in each agent's CLAUDE.md.

Overload signaling:
- If your queue grows consistently, deadlines slip, or a work pattern indicates the role is too broad, signal it: as **root**, via `ask_human` proposing an increase in `pool_size`, splitting (new role focused on a subdomain) or a specialized agent; as **child**, describe in the reply in a format ready for the parent to forward.
- Role-specific overload signals live in each agent's CLAUDE.md.
