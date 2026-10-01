"""Minimal MCP server on top of aiohttp.

Protocol: JSON-RPC 2.0, streamable HTTP transport:
  - Short tools (initialize, tools/list, memory_*, complete_phase, etc):
    return Content-Type: application/json (simple request-response).
  - Long-running blocking tools (ask_human, ask_agent): return
    Content-Type: text/event-stream and emit SSE keepalive comments
    (": ping") every SSE_KEEPALIVE_SEC until the final result. This avoids
    the ~60s timeout of the Claude Code CLI MCP client on tool calls that
    block for hours waiting for a human/agent. See D-47 in
    .dev/notes/DECISIONS.md.

Endpoint: POST /mcp/<topic_slug>

Each `claude` subprocess spawned by the ClaudeRunner gets an --mcp-config
pointing to http://localhost:<port>/mcp/<topic-slug>.
The slug correlates tool calls with the right topic.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web

from ..log import get_logger

from ..memory_store import MemoryStore
from .broker import McpBroker
from .tools import ALL_TOOLS
from .workflow import WorkflowManager

log = get_logger(__name__)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "ai-company-mcp"
SERVER_VERSION = "0.1.0"

# Tools that block >60s — delivered via SSE to keep the connection alive
# while the tool has not resolved yet. Other tools use plain JSON.
LONG_RUNNING_TOOLS = frozenset({"ask_human", "ask_agent", "ask_agents_many"})
SSE_KEEPALIVE_SEC = 20  # < 60s (observed CLI cap) with margin


JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


def _ok(id_: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _err(id_: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": error}


class McpServer:
    """aiohttp wrapper. Runs on the same event loop as the BaseAgent."""

    def __init__(
        self,
        broker: McpBroker,
        agent_name: str,
        workflow: WorkflowManager | None = None,
        memory: MemoryStore | None = None,
        host: str = "127.0.0.1",
        port: int = 8765,
        db_pool=None,
    ):
        self.broker = broker
        self.workflow = workflow
        self.memory = memory
        self.agent_name = agent_name
        self.host = host
        self.port = port
        # D-96: pool used to look up, at runtime, the parent_conv_id of the conv
        # tied to the slug, to gate ask_human/ask_agent when the conv is a child.
        self.db_pool = db_pool
        self._app = web.Application()
        self._app.router.add_post("/mcp/{slug}", self._handle_post)
        self._app.router.add_get("/mcp/{slug}", self._handle_get)  # no-op for SSE
        self._app.router.add_get("/health", self._health)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        log.info("mcp.server.started", host=self.host, port=self.port)

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
        if self._runner is not None:
            await self._runner.cleanup()

    @property
    def url_for(self) -> Callable[[str], str]:
        return lambda slug: f"http://{self.host}:{self.port}/mcp/{slug}"

    # ---------- handlers ----------

    async def _health(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _handle_get(self, _request: web.Request) -> web.Response:
        # May be used for SSE in the future. For now, 204.
        return web.Response(status=204)

    async def _handle_post(self, request: web.Request) -> web.Response:
        slug = request.match_info["slug"]
        try:
            body = await request.json()
        except Exception:
            return web.json_response(_err(None, JSONRPC_PARSE_ERROR, "Parse error"))

        # May be a batch (list) or a single request
        if isinstance(body, list):
            responses = []
            for item in body:
                resp = await self._dispatch(slug, item)
                if resp is not None:
                    responses.append(resp)
            if not responses:
                return web.Response(status=202)  # notifications only, no response
            return web.json_response(responses)

        # Long-running tool: stream SSE with keepalive until resolved.
        # The Claude CLI MCP client has a ~60s cap; without SSE, a tool call
        # blocking for hours (ask_human/ask_agent waiting for a human) gets
        # dropped. See D-47.
        if self._is_long_running_tool_call(body):
            return await self._handle_streaming_tool_call(request, slug, body)

        resp = await self._dispatch(slug, body)
        if resp is None:
            return web.Response(status=202)
        return web.json_response(resp)

    @staticmethod
    def _is_long_running_tool_call(body: Any) -> bool:
        if not isinstance(body, dict):
            return False
        if body.get("method") != "tools/call":
            return False
        params = body.get("params") or {}
        return params.get("name") in LONG_RUNNING_TOOLS

    async def _handle_streaming_tool_call(
        self, request: web.Request, slug: str, body: dict[str, Any],
    ) -> web.StreamResponse:
        """Responds with text/event-stream. Emits an SSE comment (': ping')
        every SSE_KEEPALIVE_SEC while the blocking dispatch runs, and at the
        end emits the JSON-RPC envelope as the single data event. This keeps
        the HTTP connection alive within the CLI cap (~60s without bytes).
        """
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )
        await resp.prepare(request)

        method = body.get("method")
        tool_name = ((body.get("params") or {}).get("name")) or "?"
        req_id = body.get("id")
        log.info("mcp.sse.start", slug=slug, method=method, tool=tool_name, id=req_id)

        # Run the dispatch in a task; keepalive loop in parallel.
        dispatch_task = asyncio.create_task(self._dispatch(slug, body))

        try:
            while True:
                try:
                    result = await asyncio.wait_for(
                        asyncio.shield(dispatch_task), timeout=SSE_KEEPALIVE_SEC,
                    )
                    break
                except asyncio.TimeoutError:
                    # Still running — send keepalive and continue.
                    try:
                        await resp.write(b": ping\n\n")
                    except (ConnectionResetError, asyncio.CancelledError):
                        # Client disconnected — cancel dispatch so it does not leak.
                        dispatch_task.cancel()
                        log.info("mcp.sse.client_disconnected", slug=slug, tool=tool_name)
                        return resp
            if result is not None:
                # Follows the JSON-RPC response-as-SSE format: 1 data event
                # with the envelope. MCP clients that accept SSE read this.
                payload = json.dumps(result, ensure_ascii=False)
                await resp.write(f"data: {payload}\n\n".encode("utf-8"))
            else:
                # Notification (no id) — no response, just 202-ish over SSE.
                pass
        except Exception:
            log.exception("mcp.sse.dispatch_failed", slug=slug, tool=tool_name)
            try:
                err = _err(req_id, JSONRPC_INTERNAL_ERROR, "dispatch failed")
                await resp.write(f"data: {json.dumps(err)}\n\n".encode("utf-8"))
            except Exception:
                pass
        finally:
            try:
                await resp.write_eof()
            except Exception:
                pass
            log.info("mcp.sse.end", slug=slug, tool=tool_name, id=req_id)
        return resp

    # ---------- dispatch JSON-RPC ----------

    async def _dispatch(self, slug: str, req: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(req, dict) or req.get("jsonrpc") != "2.0":
            return _err(req.get("id") if isinstance(req, dict) else None, JSONRPC_INVALID_REQUEST, "Invalid JSON-RPC request")

        method = req.get("method")
        req_id = req.get("id")
        params = req.get("params") or {}
        is_notification = "id" not in req

        log.debug("mcp.request", slug=slug, method=method, id=req_id, is_notification=is_notification)

        try:
            if method == "initialize":
                return _ok(req_id, {
                    "protocolVersion": PROTOCOL_VERSION,
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "capabilities": {"tools": {"listChanged": False}},
                })

            if method == "notifications/initialized":
                # client-to-server ack, no response
                return None

            if method == "ping":
                return _ok(req_id, {})

            if method == "tools/list":
                return _ok(req_id, {"tools": ALL_TOOLS})

            if method == "tools/call":
                return await self._call_tool(slug, req_id, params)

            if is_notification:
                # unsupported notification: ignore silently
                return None

            return _err(req_id, JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}")
        except Exception as e:
            log.exception("mcp.dispatch_error", slug=slug, method=method)
            if is_notification:
                return None
            return _err(req_id, JSONRPC_INTERNAL_ERROR, f"Internal error: {e!s}")

    async def _is_child_conv(self, slug: str) -> bool:
        """D-96: True if the conv tied to the current slug is a child
        (parent_conv_id IS NOT NULL). Used to gate tools that may only be
        called in a root conv (ask_human, ask_agent, ask_agents_many).
        Fail-open: if it cannot tell (no pool, unknown conv_id),
        returns False — keeps the old behavior instead of blocking
        unfairly."""
        if self.db_pool is None or self.broker is None:
            return False
        conv_id = self.broker.conv_id_for_slug(slug)
        if conv_id is None:
            return False
        try:
            async with self.db_pool.acquire() as conn:
                pid = await conn.fetchval(
                    "SELECT parent_conv_id FROM messaging.conversations WHERE id = $1",
                    conv_id,
                )
            return pid is not None
        except Exception:
            log.exception("mcp.is_child_conv.lookup_failed", slug=slug, conv_id=conv_id)
            return False

    @staticmethod
    def _child_gate_message(tool_name: str) -> str:
        return (
            f"`{tool_name}` is not available in a child conv (D-96): the "
            "hierarchy is root -> child, max 1 level. You are in a child conv — "
            "reply to the parent agent describing what you need (human input, "
            "consultation with another agent, etc) and they decide whether to escalate."
        )

    # ---------- Skills (per-agent) ----------
    # Canonical path in the container: /app/agents/<self.agent_name>/skills/<slug>/SKILL.md.
    # On the host it is `instance/agents/<name>/skills/<slug>/SKILL.md` via mount.
    # The entrypoint symlinks ~/.claude/skills -> /app/agents/<name>/skills/, so
    # the CLI loads these skills at startup of the next turn.
    SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
    SKILL_BODY_LIMIT = 100 * 1024  # 100KB
    SKILL_DESC_LIMIT = 500

    def _skills_root(self) -> Path:
        """Root directory of THIS agent's skills. Always derived from
        self.agent_name — the agent never passes a path. Ensures isolation."""
        return Path("/app/agents") / self.agent_name / "skills"

    def _skill_dir(self, name: str) -> Path:
        """Directory of a validated skill. Resolves it and checks the result
        is still under _skills_root (defense in depth against traversal,
        although the regex already blocks `/` and `..`)."""
        root = self._skills_root().resolve()
        candidate = (root / name).resolve()
        # In containers `Path.resolve()` follows symlinks; check via
        # `is_relative_to` to prevent escape.
        if not candidate.is_relative_to(root):
            raise ValueError(f"skill name escapes skills root: {name!r}")
        return candidate

    def _validate_skill_name(self, name: Any) -> str | None:
        if not isinstance(name, str) or not self.SKILL_NAME_RE.match(name):
            return (
                "skill name must match ^[a-z0-9][a-z0-9-]{0,49}$ "
                "(lowercase alphanumeric + hyphens, max 50 chars, must start "
                "with alphanumeric)"
            )
        return None

    def _save_skill(self, name: str, description: str, body: str) -> str:
        """Writes <skills_root>/<name>/SKILL.md (upsert). Returns 'created'
        or 'updated'."""
        skill_dir = self._skill_dir(name)
        existed = skill_dir.exists()
        skill_dir.mkdir(parents=True, exist_ok=True)
        # Minimal frontmatter + body. `marked` in the Claude CLI parses YAML
        # frontmatter; description/name come from it, not from the body.
        frontmatter = f"---\nname: {name}\ndescription: {description}\n---\n"
        content = frontmatter + (body if body.endswith("\n") else body + "\n")
        skill_path = skill_dir / "SKILL.md"
        skill_path.write_text(content, encoding="utf-8")
        return "updated" if existed else "created"

    def _list_skills(self) -> list[dict[str, str]]:
        root = self._skills_root()
        if not root.exists():
            return []
        items: list[dict[str, str]] = []
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue
            description = ""
            # Read just enough to extract `description:` from the frontmatter.
            # If missing, return an empty string — do not fail the list.
            try:
                head = skill_md.read_text(encoding="utf-8").splitlines()[:10]
                for line in head:
                    if line.lower().startswith("description:"):
                        description = line.split(":", 1)[1].strip()
                        break
            except Exception:
                pass
            items.append({"name": entry.name, "description": description})
        return items

    def _delete_skill(self, name: str) -> bool:
        skill_dir = self._skill_dir(name)
        if not skill_dir.exists():
            return False
        shutil.rmtree(skill_dir)
        return True

    async def _call_tool(self, slug: str, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}

        # D-96 gating: ask_human/ask_agent/ask_agents_many blocked in a child conv.
        if name in ("ask_human", "ask_agent", "ask_agents_many"):
            if await self._is_child_conv(slug):
                log.info("mcp.child_conv_gate", slug=slug, tool=name)
                return _err(req_id, JSONRPC_INVALID_PARAMS, self._child_gate_message(name))

        if name == "ask_human":
            topic = self.broker.topic_for_slug(slug)
            if topic is None:
                return _err(req_id, JSONRPC_INVALID_PARAMS, f"Unknown topic slug: {slug}")
            question = arguments.get("question")
            if not question:
                return _err(req_id, JSONRPC_INVALID_PARAMS, "'question' is required")
            context = arguments.get("context") or ""

            result_text = await self.broker.ask_human(
                key=topic,
                question=question,
                context=context,
            )
            return _ok(req_id, {
                "content": [{"type": "text", "text": result_text}],
                "isError": False,
            })

        if name == "ask_agent":
            # current topic (of the asker) — only used for logs
            asker_topic = self.broker.topic_for_slug(slug)
            target_agent = (arguments.get("target_agent") or "").strip()
            question = arguments.get("question")
            context = arguments.get("context") or ""
            timeout_min = float(arguments.get("timeout_minutes") or 30)
            if not target_agent or not question:
                return _err(req_id, JSONRPC_INVALID_PARAMS, "target_agent and question are required")
            if target_agent == self.agent_name:
                return _err(req_id, JSONRPC_INVALID_PARAMS, "cannot ask myself")

            try:
                response = await self.broker.ask_agent_via_callback(
                    asker_topic=asker_topic,
                    from_agent=self.agent_name,
                    target_agent=target_agent,
                    question=question,
                    context=context,
                    timeout_minutes=timeout_min,
                )
            except Exception as e:
                log.exception("mcp.ask_agent.failed", target=target_agent)
                return _err(req_id, JSONRPC_INTERNAL_ERROR, f"ask_agent failed: {e}")
            return _ok(req_id, {
                "content": [{"type": "text", "text": response}],
                "isError": False,
            })

        if name == "ask_agents_many":
            asker_topic = self.broker.topic_for_slug(slug)
            asks_raw = arguments.get("asks")
            timeout_min = float(arguments.get("timeout_minutes") or 30)
            if not isinstance(asks_raw, list) or len(asks_raw) < 2:
                return _err(
                    req_id, JSONRPC_INVALID_PARAMS,
                    "asks must be a list with >=2 items — use ask_agent for a single target",
                )
            if len(asks_raw) > 10:
                return _err(
                    req_id, JSONRPC_INVALID_PARAMS,
                    "asks has a limit of 10 items per call",
                )
            normalized: list[dict[str, str]] = []
            for i, a in enumerate(asks_raw):
                if not isinstance(a, dict):
                    return _err(req_id, JSONRPC_INVALID_PARAMS, f"asks[{i}] is not an object")
                target = (a.get("target_agent") or "").strip()
                question = a.get("question")
                context = a.get("context") or ""
                if not target or not question:
                    return _err(
                        req_id, JSONRPC_INVALID_PARAMS,
                        f"asks[{i}]: target_agent and question are required",
                    )
                if target == self.agent_name:
                    return _err(
                        req_id, JSONRPC_INVALID_PARAMS,
                        f"asks[{i}]: cannot ask myself ({target})",
                    )
                normalized.append({
                    "target_agent": target,
                    "question": question,
                    "context": context,
                })

            try:
                results = await self.broker.ask_agents_many(
                    asker_topic=asker_topic,
                    from_agent=self.agent_name,
                    asks=normalized,
                    timeout_minutes=timeout_min,
                )
            except Exception as e:
                log.exception("mcp.ask_agents_many.failed")
                return _err(req_id, JSONRPC_INTERNAL_ERROR, f"ask_agents_many failed: {e}")

            parts = [f"# Responses from {len(results)} agents\n"]
            for r in results:
                header = f"## `{r['target_agent']}`"
                if "error" in r:
                    parts.append(f"{header} — **[ERROR]**\n\n{r['error']}")
                else:
                    parts.append(f"{header}\n\n{r['response']}")
            text = "\n\n---\n\n".join(parts)
            return _ok(req_id, {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            })

        if name == "complete_phase":
            if self.workflow is None:
                return _err(req_id, JSONRPC_INTERNAL_ERROR, "workflow manager not configured on this agent")
            task_slug = arguments.get("task_slug")
            artifact = arguments.get("artifact")
            summary = arguments.get("summary") or ""
            next_ = arguments.get("next")
            next_agent = arguments.get("next_agent")
            next_topic = arguments.get("next_topic")
            if not task_slug or not artifact or not next_:
                return _err(req_id, JSONRPC_INVALID_PARAMS, "task_slug, artifact and next are required")

            # Capture the task origin from the caller's current topic (used by
            # the reactor to post the terminal notification there too).
            caller_topic = self.broker.topic_for_slug(slug) if self.broker else None
            origin_stream = caller_topic.stream if caller_topic else None
            origin_topic = caller_topic.topic if caller_topic else None

            try:
                result = await self.workflow.complete_phase(
                    task_slug=task_slug,
                    artifact=artifact,
                    summary=summary,
                    next_=next_,
                    agent_name=self.agent_name,
                    next_agent=next_agent,
                    next_topic=next_topic,
                    origin_stream=origin_stream,
                    origin_topic=origin_topic,
                    title=arguments.get("title"),
                    workflow=arguments.get("workflow"),
                    complexity=arguments.get("complexity"),
                    impact=arguments.get("impact"),
                    difficulty=arguments.get("difficulty"),
                    origin=arguments.get("origin"),
                    baseline=arguments.get("baseline"),
                    standalone=bool(arguments.get("standalone", False)),
                )
            except ValueError as e:
                return _err(req_id, JSONRPC_INVALID_PARAMS, str(e))
            except FileNotFoundError as e:
                return _err(req_id, JSONRPC_INVALID_PARAMS, str(e))
            except Exception as e:
                log.exception("mcp.complete_phase.failed", task_slug=task_slug)
                return _err(req_id, JSONRPC_INTERNAL_ERROR, f"internal error: {e}")

            lines = [
                f"OK. from_step='{result['from_step']}' next='{result['next']}' "
                f"status='{result['status']}' event={result['event_id']}.",
                "",
                result["guidance"],
            ]
            return _ok(req_id, {
                "content": [{"type": "text", "text": "\n".join(lines)}],
                "isError": False,
            })

        if name == "init_repo":
            if self.workflow is None:
                return _err(req_id, JSONRPC_INTERNAL_ERROR, "workflow manager not configured on this agent")
            repo_name = arguments.get("name")
            if not repo_name:
                return _err(req_id, JSONRPC_INVALID_PARAMS, "name is required")
            try:
                result = await self.workflow.init_repo(
                    name=repo_name,
                    agent_name=self.agent_name,
                )
            except ValueError as e:
                return _err(req_id, JSONRPC_INVALID_PARAMS, str(e))
            except Exception as e:
                log.exception("mcp.init_repo.failed", repo=repo_name)
                return _err(req_id, JSONRPC_INTERNAL_ERROR, f"internal error: {e}")
            status = "created" if result["created"] else "already exists"
            msg = f"Repo '{repo_name}' {status}. HEAD={result['head_sha'][:8]}.\n\n{result['guidance']}"
            return _ok(req_id, {"content": [{"type": "text", "text": msg}], "isError": False})

        if name == "create_worktree":
            if self.workflow is None:
                return _err(req_id, JSONRPC_INTERNAL_ERROR, "workflow manager not configured on this agent")
            task_slug = arguments.get("task_slug")
            repo = arguments.get("repo")
            baseline_sha = arguments.get("baseline_sha")
            branch = arguments.get("branch")
            if not task_slug or not repo:
                return _err(req_id, JSONRPC_INVALID_PARAMS,
                            "task_slug and repo are required")
            try:
                result = await self.workflow.create_worktree(
                    task_slug=task_slug, repo=repo,
                    baseline_sha=baseline_sha or None,
                    branch=branch or None,
                    agent_name=self.agent_name,
                )
            except ValueError as e:
                return _err(req_id, JSONRPC_INVALID_PARAMS, str(e))
            except Exception as e:
                log.exception("mcp.create_worktree.failed", task_slug=task_slug)
                return _err(req_id, JSONRPC_INTERNAL_ERROR, f"internal error: {e}")
            wt = result["worktree"]
            header = (
                f"OK. worktree '{wt['repo']}' (branch {wt['branch']}) "
                + ("reused" if wt.get("reused") else "created")
                + f" at {wt['path']} from {wt['baseline_sha']}."
            )
            msg = header + "\n\n" + result["guidance"]
            return _ok(req_id, {"content": [{"type": "text", "text": msg}], "isError": False})

        if name == "cleanup_worktrees":
            if self.workflow is None:
                return _err(req_id, JSONRPC_INTERNAL_ERROR, "workflow manager not configured on this agent")
            task_slug = arguments.get("task_slug")
            if not task_slug:
                return _err(req_id, JSONRPC_INVALID_PARAMS, "task_slug is required")
            try:
                result = await self.workflow.cleanup_worktrees(
                    task_slug=task_slug,
                    agent_name=self.agent_name,
                )
            except ValueError as e:
                return _err(req_id, JSONRPC_INVALID_PARAMS, str(e))
            except Exception as e:
                log.exception("mcp.cleanup_worktrees.failed", task_slug=task_slug)
                return _err(req_id, JSONRPC_INTERNAL_ERROR, f"internal error: {e}")
            removed = result.get("removed", [])
            failed = result.get("failed", [])
            lines = [
                f"OK. removed={len(removed)} failed={len(failed)}.",
                "",
                result["guidance"],
            ]
            if removed:
                lines.append("")
                lines.append("Removed:")
                for r in removed:
                    warn = f" (warnings: {r['warnings']})" if r.get("warnings") else ""
                    lines.append(f"  - {r['repo']} @ {r['path']}{warn}")
            if failed:
                lines.append("")
                lines.append("Failures:")
                for f_ in failed:
                    lines.append(f"  - {f_['repo']} @ {f_['path']}: {f_['errors']}")
            return _ok(req_id, {
                "content": [{"type": "text", "text": "\n".join(lines)}],
                "isError": bool(failed),
            })

        if name == "get_task_state":
            if self.workflow is None:
                return _err(req_id, JSONRPC_INTERNAL_ERROR, "workflow manager not configured on this agent")
            task_slug = arguments.get("task_slug")
            if not task_slug:
                return _err(req_id, JSONRPC_INVALID_PARAMS, "task_slug is required")
            try:
                state = await self.workflow.get_task_state(task_slug)
            except ValueError as e:
                return _err(req_id, JSONRPC_INVALID_PARAMS, str(e))
            except Exception as e:
                log.exception("mcp.get_task_state.failed", task_slug=task_slug)
                return _err(req_id, JSONRPC_INTERNAL_ERROR, f"internal error: {e}")
            import json as _json
            return _ok(req_id, {
                "content": [{"type": "text", "text": _json.dumps(state, indent=2, ensure_ascii=False)}],
                "isError": False,
            })

        if name == "reopen_task":
            if self.workflow is None:
                return _err(req_id, JSONRPC_INTERNAL_ERROR, "workflow manager not configured on this agent")
            task_slug = arguments.get("task_slug")
            next_step = arguments.get("next_step")
            next_agent = arguments.get("next_agent")
            reason = arguments.get("reason")
            if not task_slug or not next_step or not reason:
                return _err(req_id, JSONRPC_INVALID_PARAMS,
                            "task_slug, next_step and reason are required")
            try:
                result = await self.workflow.reopen_task(
                    task_slug=task_slug,
                    agent_name=self.agent_name,
                    next_step=next_step,
                    next_agent=next_agent,
                    reason=reason,
                    standalone=bool(arguments.get("standalone", False)),
                )
            except ValueError as e:
                return _err(req_id, JSONRPC_INVALID_PARAMS, str(e))
            except Exception as e:
                log.exception("mcp.reopen_task.failed", task_slug=task_slug)
                return _err(req_id, JSONRPC_INTERNAL_ERROR, f"internal error: {e}")
            lines = [
                f"OK. Task reopened: prev_status='{result['prev_status']}' → status='in_progress', "
                f"next_step='{result['next_step']}' next_agent='{result['next_agent']}' event={result['event_id']}.",
                "",
                result["guidance"],
            ]
            return _ok(req_id, {
                "content": [{"type": "text", "text": "\n".join(lines)}],
                "isError": False,
            })

        if name == "notify_human":
            topic = self.broker.topic_for_slug(slug) if self.broker else None
            if topic is None:
                return _err(req_id, JSONRPC_INVALID_PARAMS, f"Unknown topic slug: {slug}")
            message = (arguments.get("message") or "").strip()
            if not message:
                return _err(req_id, JSONRPC_INVALID_PARAMS, "'message' is required")
            try:
                await self.broker.notify_human(topic, message)
            except Exception as e:
                log.exception("mcp.notify_human.failed")
                return _err(req_id, JSONRPC_INTERNAL_ERROR, f"notify failed: {e}")
            return _ok(req_id, {
                "content": [{"type": "text", "text": "OK. Message posted to the current conversation (no pending_ask)."}],
                "isError": False,
            })

        if name == "archive_conversation":
            if self.broker is None:
                return _err(req_id, JSONRPC_INTERNAL_ERROR, "broker not configured")
            conv_id = self.broker.conv_id_for_slug(slug)
            if conv_id is None:
                return _err(
                    req_id, JSONRPC_INVALID_PARAMS,
                    f"unknown conv_id for topic slug {slug!r} — please open an issue.",
                )
            try:
                await self.broker.archive_conversation(conv_id)
            except Exception as e:
                log.exception("mcp.archive_conversation.failed")
                return _err(req_id, JSONRPC_INTERNAL_ERROR, f"archive failed: {e}")
            return _ok(req_id, {
                "content": [{"type": "text", "text": (
                    "OK. Conversation archived — moves to the Closed tab for the human. "
                    "Descendants (ask_agent/task children) were also archived."
                )}],
                "isError": False,
            })

        if name in ("memory_save", "memory_recall", "memory_list", "memory_edit", "memory_delete"):
            if self.memory is None:
                return _err(req_id, JSONRPC_INTERNAL_ERROR, "memory not configured on this agent")
            try:
                if name == "memory_save":
                    key = arguments.get("key")
                    value = arguments.get("value")
                    if not key or not value:
                        return _err(req_id, JSONRPC_INVALID_PARAMS, "key and value are required")
                    tags = arguments.get("tags") or []
                    result = await self.memory.save(key=key, value=value, tags=tags)
                    msg = f"{result['action']}: {key}"
                    if result["action"] == "created" and (await self.memory.count()) == 1:
                        # first fact — educational hint
                        msg += " (first entry in memory)"
                    return _ok(req_id, {"content": [{"type": "text", "text": msg}], "isError": False})

                if name == "memory_recall":
                    query = arguments.get("query") or ""
                    limit = int(arguments.get("limit") or 5)
                    items = await self.memory.recall(query, limit=limit)
                    if not items:
                        text = f"(no relevant facts for '{query}' in memory)"
                    else:
                        lines = [f"{len(items)} fact(s):"]
                        for it in items:
                            tag_str = f" [{','.join(it['tags'])}]" if it["tags"] else ""
                            lines.append(f"- {it['key']}{tag_str}: {it['value']}")
                        text = "\n".join(lines)
                    return _ok(req_id, {"content": [{"type": "text", "text": text}], "isError": False})

                if name == "memory_list":
                    limit = int(arguments.get("limit") or 20)
                    tag = arguments.get("tag")
                    items = await self.memory.list_recent(limit=limit, tag=tag)
                    if not items:
                        text = "(memory is empty)"
                    else:
                        lines = [f"{len(items)} recent fact(s):"]
                        for it in items:
                            tag_str = f" [{','.join(it['tags'])}]" if it["tags"] else ""
                            lines.append(f"- {it['key']}{tag_str}: {it['value'][:120]}")
                        text = "\n".join(lines)
                    return _ok(req_id, {"content": [{"type": "text", "text": text}], "isError": False})

                if name == "memory_edit":
                    key = arguments.get("key")
                    if not key:
                        return _err(req_id, JSONRPC_INVALID_PARAMS, "key is required")
                    value = arguments.get("value")
                    tags = arguments.get("tags")
                    if value is None and tags is None:
                        return _err(
                            req_id,
                            JSONRPC_INVALID_PARAMS,
                            "provide 'value' and/or 'tags' to edit",
                        )
                    try:
                        await self.memory.edit(key=key, value=value, tags=tags)
                    except KeyError:
                        return _err(
                            req_id,
                            JSONRPC_INVALID_PARAMS,
                            f"key '{key}' does not exist — use memory_save to create",
                        )
                    return _ok(
                        req_id,
                        {"content": [{"type": "text", "text": f"updated: {key}"}], "isError": False},
                    )

                if name == "memory_delete":
                    key = arguments.get("key")
                    if not key:
                        return _err(req_id, JSONRPC_INVALID_PARAMS, "key is required")
                    removed = await self.memory.delete(key)
                    if not removed:
                        return _err(
                            req_id,
                            JSONRPC_INVALID_PARAMS,
                            f"key '{key}' does not exist in memory",
                        )
                    return _ok(
                        req_id,
                        {"content": [{"type": "text", "text": f"deleted: {key}"}], "isError": False},
                    )
            except Exception as e:
                log.exception("mcp.memory.failed", tool=name)
                return _err(req_id, JSONRPC_INTERNAL_ERROR, f"memory error: {e}")

        if name in ("backlog_add", "backlog_list", "backlog_update", "backlog_promote", "task_list"):
            if self.workflow is None:
                return _err(req_id, JSONRPC_INTERNAL_ERROR, "workflow not configured (no db_pool)")
            try:
                result = await self._handle_task_store(name, arguments)
            except ValueError as e:
                return _err(req_id, JSONRPC_INVALID_PARAMS, str(e))
            except Exception as e:
                log.exception("mcp.task_store.failed", tool=name)
                return _err(req_id, JSONRPC_INTERNAL_ERROR, f"internal error: {e}")
            return _ok(req_id, result)

        if name in ("schedule_add", "schedule_list", "schedule_update", "schedule_remove"):
            if self.workflow is None:
                return _err(req_id, JSONRPC_INTERNAL_ERROR, "workflow not configured (no db_pool)")
            try:
                result = await self._handle_schedule_store(name, arguments)
            except ValueError as e:
                return _err(req_id, JSONRPC_INVALID_PARAMS, str(e))
            except Exception as e:
                log.exception("mcp.schedule_store.failed", tool=name)
                return _err(req_id, JSONRPC_INTERNAL_ERROR, f"internal error: {e}")
            return _ok(req_id, result)

        if name in ("save_skill", "list_skills", "delete_skill"):
            try:
                if name == "save_skill":
                    skill_name = arguments.get("name")
                    err = self._validate_skill_name(skill_name)
                    if err:
                        return _err(req_id, JSONRPC_INVALID_PARAMS, err)
                    description = arguments.get("description")
                    body = arguments.get("body")
                    if not isinstance(description, str) or not description.strip():
                        return _err(req_id, JSONRPC_INVALID_PARAMS, "description is required")
                    if len(description) > self.SKILL_DESC_LIMIT:
                        return _err(
                            req_id,
                            JSONRPC_INVALID_PARAMS,
                            f"description exceeds {self.SKILL_DESC_LIMIT} chars",
                        )
                    if "\n" in description:
                        return _err(
                            req_id,
                            JSONRPC_INVALID_PARAMS,
                            "description must be a single line (no newlines)",
                        )
                    if not isinstance(body, str) or not body.strip():
                        return _err(req_id, JSONRPC_INVALID_PARAMS, "body is required")
                    if len(body.encode("utf-8")) > self.SKILL_BODY_LIMIT:
                        return _err(
                            req_id,
                            JSONRPC_INVALID_PARAMS,
                            f"body exceeds {self.SKILL_BODY_LIMIT} bytes",
                        )
                    action = self._save_skill(skill_name, description.strip(), body)
                    text = (
                        f"{action}: {skill_name} (available on the next turn — "
                        "Claude CLI loads skills at startup, not mid-session)"
                    )
                    return _ok(req_id, {"content": [{"type": "text", "text": text}], "isError": False})

                if name == "list_skills":
                    items = self._list_skills()
                    if not items:
                        text = "(no skills saved for this agent yet)"
                    else:
                        lines = [f"{len(items)} skill(s):"]
                        for it in items:
                            desc = it["description"] or "(no description)"
                            lines.append(f"- {it['name']}: {desc}")
                        text = "\n".join(lines)
                    return _ok(req_id, {"content": [{"type": "text", "text": text}], "isError": False})

                if name == "delete_skill":
                    skill_name = arguments.get("name")
                    err = self._validate_skill_name(skill_name)
                    if err:
                        return _err(req_id, JSONRPC_INVALID_PARAMS, err)
                    removed = self._delete_skill(skill_name)
                    if not removed:
                        return _err(
                            req_id,
                            JSONRPC_INVALID_PARAMS,
                            f"skill '{skill_name}' does not exist",
                        )
                    return _ok(
                        req_id,
                        {"content": [{"type": "text", "text": f"deleted: {skill_name}"}], "isError": False},
                    )
            except ValueError as e:
                return _err(req_id, JSONRPC_INVALID_PARAMS, str(e))
            except Exception as e:
                log.exception("mcp.skill.failed", tool=name)
                return _err(req_id, JSONRPC_INTERNAL_ERROR, f"skill error: {e}")

        return _err(req_id, JSONRPC_METHOD_NOT_FOUND, f"Unknown tool: {name}")

    # ---------- Backlog + Task list store (D-53) ----------
    # These tools talk to Postgres directly (via self.workflow._pool),
    # bypassing the WorkflowManager — they are management operations (backlog
    # CRUD, task list) that do not move workflow state.

    async def _handle_task_store(self, name: str, args: dict) -> dict[str, Any]:
        import json as _json
        import re as _re
        SLUG_RE = _re.compile(r"^[a-z0-9][a-z0-9-]*$")
        pool = self.workflow._pool
        agent_name = self.agent_name

        if name == "backlog_add":
            slug = (args.get("slug") or "").strip()
            title = (args.get("title") or "").strip()
            if not SLUG_RE.match(slug):
                raise ValueError(f"invalid slug: {slug!r}. Use kebab-case.")
            if not title:
                raise ValueError("title is required")
            priority = int(args.get("priority") or 0)
            content = args.get("content") or ""
            impact = args.get("impact")
            effort = args.get("effort")
            status = (args.get("status") or "open").strip()
            if status not in ("open", "draft", "in_progress", "promoted", "discarded"):
                raise ValueError(
                    f"invalid status: {status!r}. "
                    "Use open | draft | in_progress | promoted | discarded."
                )
            row = await pool.fetchrow(
                """INSERT INTO tasks.backlog
                    (slug, title, content, priority, impact, effort, status, created_by)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                   ON CONFLICT (slug) DO UPDATE SET
                     title = EXCLUDED.title,
                     content = EXCLUDED.content,
                     priority = EXCLUDED.priority,
                     impact = EXCLUDED.impact,
                     effort = EXCLUDED.effort,
                     status = EXCLUDED.status
                   RETURNING id, slug, status, priority""",
                slug, title, content, priority, impact, effort, status, agent_name,
            )
            if row["status"] == "draft":
                text = (
                    f"OK. Draft '{row['slug']}' captured "
                    f"(priority={row['priority']}, status=draft).\n"
                    "Does not show up in the default backlog; surfaces in daily curation "
                    "or via the 'Draft'/status='draft' filter. Use backlog_update "
                    "to specify it later."
                )
            else:
                text = (
                    f"OK. Backlog item '{row['slug']}' registered "
                    f"(priority={row['priority']}, status={row['status']}).\n"
                    "Visible in PWA Backlog. Use backlog_promote to turn it into a task."
                )
            return {"content": [{"type": "text", "text": text}], "isError": False}

        if name == "backlog_list":
            status = (args.get("status") or "open").strip()
            limit = min(int(args.get("limit") or 50), 500)
            if status == "all":
                rows = await pool.fetch(
                    """SELECT slug, title, priority, impact, effort, status,
                              promoted_task_slug, created_by, updated_at
                         FROM tasks.backlog
                        ORDER BY priority DESC, updated_at DESC
                        LIMIT $1""",
                    limit,
                )
            else:
                rows = await pool.fetch(
                    """SELECT slug, title, priority, impact, effort, status,
                              promoted_task_slug, created_by, updated_at
                         FROM tasks.backlog
                        WHERE status = $1
                        ORDER BY priority DESC, updated_at DESC
                        LIMIT $2""",
                    status, limit,
                )
            if not rows:
                text = f"(empty backlog for status='{status}')"
            else:
                lines = [f"{len(rows)} backlog item(s) (status={status}):"]
                for r in rows:
                    prio_label = {-2:"very-low",-1:"low",0:"normal",1:"high",2:"critical"}.get(
                        int(r["priority"] or 0), str(r["priority"]),
                    )
                    tag = f" [{prio_label}]"
                    if r["impact"]:
                        tag += f" impact={r['impact']}"
                    if r["effort"]:
                        tag += f" effort={r['effort']}"
                    lines.append(f"- {r['slug']}: {r['title']}{tag}")
                text = "\n".join(lines)
            return {"content": [{"type": "text", "text": text}], "isError": False}

        if name == "backlog_update":
            slug = (args.get("slug") or "").strip()
            if not SLUG_RE.match(slug):
                raise ValueError(f"invalid slug: {slug!r}")
            if args.get("status") and args["status"] not in (
                "open", "draft", "in_progress", "promoted", "discarded",
            ):
                raise ValueError(
                    f"invalid status: {args['status']!r}. "
                    "Use open | draft | in_progress | promoted | discarded."
                )
            fields = []
            params: list = []
            idx = 1
            for col in ("title", "content", "impact", "effort", "status"):
                if col in args and args[col] is not None:
                    fields.append(f"{col} = ${idx}")
                    params.append(args[col])
                    idx += 1
            if "priority" in args and args["priority"] is not None:
                fields.append(f"priority = ${idx}")
                params.append(int(args["priority"]))
                idx += 1
            if not fields:
                raise ValueError("nothing to update")
            params.append(slug)
            sql = f"UPDATE tasks.backlog SET {', '.join(fields)} WHERE slug = ${idx} RETURNING slug, status, priority"
            row = await pool.fetchrow(sql, *params)
            if row is None:
                raise ValueError(f"backlog item '{slug}' does not exist")
            text = f"OK. '{row['slug']}' updated (status={row['status']}, priority={row['priority']})."
            return {"content": [{"type": "text", "text": text}], "isError": False}

        if name == "backlog_promote":
            backlog_slug = (args.get("slug") or "").strip()
            if not SLUG_RE.match(backlog_slug):
                raise ValueError(f"invalid slug: {backlog_slug!r}")
            task_slug = (args.get("task_slug") or backlog_slug).strip()
            if not SLUG_RE.match(task_slug):
                raise ValueError(f"invalid task_slug: {task_slug!r}")
            workflow = args.get("workflow")
            next_agent = args.get("next_agent")
            initial_topic = (args.get("initial_topic") or f"task-{task_slug}").strip()

            # Resolve initial_step + dispatch_agent BEFORE the transaction — used
            # both to set current_step on the task and for the event payload.
            wf = self.workflow.registry.get(workflow) if workflow else None
            initial_step: str | None = None
            if wf and wf.initial_step:
                initial_step = wf.initial_step
                step = wf.step(initial_step)
                if step and step.agent and not next_agent:
                    next_agent = step.agent
            dispatch_agent = next_agent
            if not dispatch_agent:
                raise ValueError(
                    "could not determine initial agent. "
                    "Pass next_agent or define a workflow with initial_step.agent."
                )

            # Resolve the workflow orchestrator. It hosts the task's supervisor
            # conv — a fixed point while the task lives, regardless of who
            # promoted it or who runs each phase. With no orchestrator declared,
            # fall back to dispatch_agent (pre-orchestrator workflows keep
            # working as before — supervisor == first phase conv).
            orchestrator = (wf.orchestrator if wf else None) or dispatch_agent
            # task origin = (orchestrator, task-<slug>). The reactor uses it to
            # create the supervisor conv (Path A) or merge it with the first
            # phase conv (Path B, orchestrator == dispatch_agent).
            origin_stream = orchestrator
            origin_topic = initial_topic

            async with pool.acquire() as conn:
                async with conn.transaction():
                    item = await conn.fetchrow(
                        "SELECT title, content FROM tasks.backlog WHERE slug = $1 FOR UPDATE",
                        backlog_slug,
                    )
                    if item is None:
                        raise ValueError(f"backlog item '{backlog_slug}' does not exist")
                    # Create the task (if it already exists, do not duplicate — prefer atomic).
                    # current_step + current_agent set from the INSERT so the
                    # next_agent woken by the handoff already sees consistent state
                    # (avoids a race with claude_runner._step_instructions_block, which
                    # reads current_step from the DB at spawn).
                    existing = await conn.fetchval(
                        "SELECT id FROM tasks.tasks WHERE slug = $1", task_slug,
                    )
                    if existing is None:
                        await conn.execute(
                            """INSERT INTO tasks.tasks
                                  (slug, title, workflow, status,
                                   current_step, current_agent,
                                   origin_stream, origin_topic)
                               VALUES ($1, $2, $3, 'in_progress', $4, $5, $6, $7)""",
                            task_slug, item["title"], workflow,
                            initial_step, dispatch_agent,
                            origin_stream, origin_topic,
                        )
                    # Mark the item as promoted.
                    await conn.execute(
                        """UPDATE tasks.backlog
                              SET status = 'promoted', promoted_task_slug = $2
                            WHERE slug = $1""",
                        backlog_slug, task_slug,
                    )
                    payload = {
                        "task_slug": task_slug,
                        "from_step": None,
                        "from_agent": agent_name,
                        "artifact": None,
                        "summary": f"promoted from backlog: {item['title']}",
                        "next": "start",
                        "next_agent": dispatch_agent,
                        "next_topic": initial_topic,
                        "origin_stream": origin_stream,
                        "origin_topic": origin_topic,
                        "workflow": workflow,
                        "backlog_slug": backlog_slug,
                        "backlog_content": item["content"],
                    }
                    await conn.execute(
                        """INSERT INTO orchestrator.events
                               (emitted_by, event_type, task_slug, payload)
                           VALUES ($1, 'phase_complete', $2, $3::jsonb)""",
                        agent_name, task_slug, _json.dumps(payload),
                    )
            text = (
                f"OK. Backlog '{backlog_slug}' promoted to task '{task_slug}'. "
                f"Orchestrator: {orchestrator}. "
                f"Initial handoff: {dispatch_agent}#{initial_topic}."
            )
            return {"content": [{"type": "text", "text": text}], "isError": False}

        if name == "task_list":
            include_archived = bool(args.get("include_archived"))
            limit = min(int(args.get("limit") or 50), 500)
            slug_prefix = (args.get("slug_prefix") or "").strip() or None
            conds: list[str] = []
            params: list[Any] = []
            if not include_archived:
                conds.append("archived_at IS NULL")
            if slug_prefix:
                params.append(slug_prefix + "%")
                conds.append(f"slug LIKE ${len(params)}")
            where = f"WHERE {' AND '.join(conds)}" if conds else ""
            params.append(limit)
            rows = await pool.fetch(
                f"""SELECT slug, title, status, current_step, current_agent,
                          workflow, updated_at, archived_at,
                          (SELECT COUNT(*) FROM tasks.phases p
                             WHERE p.task_id = t.id AND p.completed_at IS NOT NULL) AS phases_count
                     FROM tasks.tasks t {where}
                    ORDER BY updated_at DESC
                    LIMIT ${len(params)}""",
                *params,
            )
            if not rows:
                text = "(no active tasks)" if not include_archived else "(no tasks)"
            else:
                lines = [f"{len(rows)} task(s):"]
                for r in rows:
                    tag = f" [{r['status']}"
                    if r["current_step"]:
                        tag += f"/{r['current_step']}"
                    if r["current_agent"]:
                        tag += f"@{r['current_agent']}"
                    tag += "]"
                    if r["archived_at"] is not None:
                        tag += " (archived)"
                    lines.append(f"- {r['slug']}: {r['title']}{tag}")
                text = "\n".join(lines)
            return {"content": [{"type": "text", "text": text}], "isError": False}

        raise ValueError(f"internal tool not implemented: {name}")

    # ---------- Scheduler custom jobs (MCP) ----------
    # Whitelist: agents may only create/edit jobs with action=post_message.
    # Native actions (backup/cleanup/cost_*) are managed in the PWA at
    # /settings/routines — if the agent needs one, it asks via ask_human.

    _AGENT_ACTION_WHITELIST = {"post_message"}
    _AGENT_MAX_JOBS_PER_CREATOR = 20

    async def _handle_schedule_store(self, name: str, args: dict) -> dict[str, Any]:
        import json as _json
        import re as _re
        SLUG_RE = _re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
        CRON_FIELD_RE = _re.compile(r"^[\d*/,\-A-Z]+$", _re.IGNORECASE)
        pool = self.workflow._pool
        agent_name = self.agent_name

        def _validate_cron(cron: str) -> None:
            parts = cron.strip().split()
            if len(parts) != 5:
                raise ValueError(
                    "invalid cron: needs 5 fields ('min hour dom month dow')"
                )
            for p in parts:
                if not CRON_FIELD_RE.match(p):
                    raise ValueError(f"invalid cron: field {p!r}")

        async def _notify(scope: str, id_: str | None) -> None:
            payload = _json.dumps({"scope": scope, "id": id_})
            await pool.execute(
                "SELECT pg_notify('scheduler_config_reload', $1)", payload
            )

        if name == "schedule_add":
            slug = (args.get("slug") or "").strip()
            if not SLUG_RE.match(slug):
                raise ValueError(f"invalid slug: {slug!r}. Use kebab-case.")
            cron = (args.get("cron") or "").strip()
            _validate_cron(cron)
            action = (args.get("action") or "").strip()
            if action not in self._AGENT_ACTION_WHITELIST:
                raise ValueError(
                    f"action {action!r} not permitted via MCP. Agents can only "
                    f"schedule: {sorted(self._AGENT_ACTION_WHITELIST)}. "
                    "For native actions (backup/cleanup/cost_check), ask the "
                    "human to configure them in the PWA at Settings -> Routines."
                )
            params = args.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            if action == "post_message":
                missing = [k for k in ("stream", "topic", "content") if not params.get(k)]
                if missing:
                    raise ValueError(
                        f"post_message requires params: stream, topic, content (missing: {missing})"
                    )
            count = await pool.fetchval(
                "SELECT COUNT(*) FROM scheduler.custom_jobs WHERE created_by = $1",
                agent_name,
            )
            if count and count >= self._AGENT_MAX_JOBS_PER_CREATOR:
                raise ValueError(
                    f"limit reached: {count} jobs already created by "
                    f"{agent_name} (cap {self._AGENT_MAX_JOBS_PER_CREATOR}). "
                    "Delete one before creating another."
                )
            existing = await pool.fetchval(
                "SELECT slug FROM scheduler.custom_jobs WHERE slug = $1", slug
            )
            if existing:
                raise ValueError(f"slug {slug!r} already exists")
            description = args.get("description")
            await pool.execute(
                """INSERT INTO scheduler.custom_jobs
                     (slug, cron, action, params, description, enabled, created_by)
                   VALUES ($1, $2, $3, $4::jsonb, $5, true, $6)""",
                slug, cron, action, _json.dumps(params), description, agent_name,
            )
            await _notify("custom", slug)
            text = (
                f"OK. Scheduled job '{slug}' created (action={action}, cron={cron}). "
                "Visible in PWA /scheduler. Hot-reloaded — no restart needed."
            )
            return {"content": [{"type": "text", "text": text}], "isError": False}

        if name == "schedule_list":
            rows = await pool.fetch(
                """SELECT slug, cron, action, params, description, enabled,
                          created_by, created_at
                     FROM scheduler.custom_jobs
                    ORDER BY slug"""
            )
            if not rows:
                text = "(no custom scheduled jobs)"
            else:
                lines = [f"{len(rows)} custom scheduled job(s):"]
                for r in rows:
                    tag = f"[{'enabled' if r['enabled'] else 'disabled'}]"
                    lines.append(
                        f"- {r['slug']} {tag} cron='{r['cron']}' action={r['action']}"
                        + (f" by {r['created_by']}" if r['created_by'] else "")
                    )
                text = "\n".join(lines)
            return {"content": [{"type": "text", "text": text}], "isError": False}

        if name == "schedule_update":
            slug = (args.get("slug") or "").strip()
            if not slug:
                raise ValueError("slug is required")
            existing = await pool.fetchrow(
                """SELECT slug, action FROM scheduler.custom_jobs WHERE slug = $1""",
                slug,
            )
            if existing is None:
                raise ValueError(f"slug {slug!r} does not exist")
            # Protect action: do not allow changing it via update (if the agent
            # wants another action, delete and recreate).
            if "action" in args:
                raise ValueError(
                    "schedule_update does not change 'action'. Remove and recreate."
                )
            sets: list[str] = []
            vals: list[Any] = []
            idx = 1
            if "cron" in args and args["cron"] is not None:
                cron = str(args["cron"]).strip()
                _validate_cron(cron)
                sets.append(f"cron = ${idx}"); vals.append(cron); idx += 1
            if "params" in args and args["params"] is not None:
                params = args["params"]
                if not isinstance(params, dict):
                    raise ValueError("params must be an object")
                sets.append(f"params = ${idx}::jsonb")
                vals.append(_json.dumps(params)); idx += 1
            if "description" in args and args["description"] is not None:
                sets.append(f"description = ${idx}"); vals.append(args["description"]); idx += 1
            if "enabled" in args and args["enabled"] is not None:
                sets.append(f"enabled = ${idx}"); vals.append(bool(args["enabled"])); idx += 1
            if not sets:
                text = f"noop: no fields to update on {slug!r}"
                return {"content": [{"type": "text", "text": text}], "isError": False}
            vals.append(slug)
            await pool.execute(
                f"UPDATE scheduler.custom_jobs SET {', '.join(sets)} WHERE slug = ${idx}",
                *vals,
            )
            await _notify("custom", slug)
            text = f"OK. Job '{slug}' updated. Hot-reloaded."
            return {"content": [{"type": "text", "text": text}], "isError": False}

        if name == "schedule_remove":
            slug = (args.get("slug") or "").strip()
            if not slug:
                raise ValueError("slug is required")
            row = await pool.fetchrow(
                "DELETE FROM scheduler.custom_jobs WHERE slug = $1 RETURNING slug",
                slug,
            )
            if row is None:
                raise ValueError(f"slug {slug!r} does not exist")
            await _notify("custom", slug)
            text = f"OK. Job '{slug}' removed."
            return {"content": [{"type": "text", "text": text}], "isError": False}

        raise ValueError(f"scheduler tool not implemented: {name}")
