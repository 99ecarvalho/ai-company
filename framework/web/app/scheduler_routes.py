"""Scheduler CRUD — custom jobs (DB-backed) + native overrides.

Complements broker.py (which proxies scheduler_http on
:8811 — health, jobs-list, pause/resume/run-now). This module is about
CONFIG: create/edit/remove jobs without a restart, keeping the framework's
native jobs (cleanup/backup/cost) separate from the instance's custom ones
(post_message and future actions).

Hot-reload: write endpoints emit `pg_notify('scheduler_config_reload',
{"scope":"custom"|"native", "id":...})`. The scheduler container has an
async task listening on that channel — diffs against `_jobs_by_id` and
adds/replaces/removes via APScheduler.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import re

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db
from .auth import Principal, get_principal, require_admin


router = APIRouter(prefix="/api/scheduler")


# ---------- Native defaults loaded from the framework YAML ----------

_DEFAULTS_PATH = Path(
    os.environ.get(
        "SCHEDULER_DEFAULTS_PATH",
        "/app/framework/orchestrator/defaults/schedule.yaml",
    )
)
_DEFAULTS_CACHE: dict[str, dict] | None = None


def _load_native_defaults() -> dict[str, dict]:
    """Reads the framework defaults YAML. Cached in memory — the file
    is immutable in the container (comes from the build). Returns {id: job_dict}."""
    global _DEFAULTS_CACHE
    if _DEFAULTS_CACHE is not None:
        return _DEFAULTS_CACHE
    if not _DEFAULTS_PATH.exists():
        # Fallback for local dev (no /app prefix).
        alt = Path("framework/orchestrator/defaults/schedule.yaml")
        if alt.exists():
            path = alt
        else:
            _DEFAULTS_CACHE = {}
            return _DEFAULTS_CACHE
    else:
        path = _DEFAULTS_PATH
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        _DEFAULTS_CACHE = {}
        return _DEFAULTS_CACHE
    jobs = data.get("jobs") or []
    _DEFAULTS_CACHE = {j["id"]: j for j in jobs if isinstance(j, dict) and j.get("id")}
    return _DEFAULTS_CACHE


# Human-readable description per action — shown in the PWA at /settings/routines.
# Keep the text short, factual, in English (PWA invariant). If you add a new
# action, add it here.
_ACTION_DESCRIPTIONS: dict[str, str] = {
    "cleanup_live_events": (
        "Delete rows from telemetry.live_events older than `days`. Keeps "
        "incident debug + recent metrics without letting the table explode."
    ),
    "cleanup_live_events_output_full": (
        "Strip the large `output_full` JSONB field from tool_result rows "
        "older than `hours`. Row stays (short inline preserved); heavy blob "
        "is dropped. 'View full' in the PWA works during the session and "
        "degrades gracefully afterwards."
    ),
    "cleanup_telemetry": (
        "Delete telemetry events (cost/tokens/latency) older than `days`. "
        "Long retention window because dashboards care about trends."
    ),
    "cleanup_sessions": "Delete expired rows from web.sessions.",
    "backup_company": (
        "tar.gz snapshot of instance/company/ (workflows, tasks, metadata). "
        "Gzipped + rotated by `retain` days. Local: instance/backups/company/."
    ),
    "backup_postgres": (
        "Full pg_dump of the framework database. Gzipped + rotated by "
        "`retain` days. Local: instance/backups/postgres/."
    ),
    "cost_budget_check": (
        "Per-agent budget check: aggregates spend from telemetry and posts "
        "alerts when any agent crosses its configured daily cap."
    ),
    "post_message": (
        "Send a message to a stream/topic. Custom jobs use this for "
        "scheduled reminders, daily standups, recurring prompts."
    ),
}


# ---------- Validation ----------

_SLUG_RE = __import__("re").compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")

# Actions the PWA can create. Agents via MCP are restricted to
# post_message — separate check in the MCP handler. A human could create
# any of the native ones as a custom job (rare, but possible), but
# we see no strong reason today; we restrict to post_message here too
# to keep the custom/native distinction clean.
_HUMAN_ACTION_WHITELIST = {"post_message"}


_CRON_FIELD_RE = re.compile(r"^[\d*/,\-A-Z]+$", re.IGNORECASE)


def _validate_cron(cron: str) -> None:
    """Light validation — checks the 5-field shape with acceptable chars. The
    full parser runs in the scheduler (CronTrigger.from_crontab) at
    registration time — here we reject obvious garbage without duplicating the
    whole engine (web doesn't need apscheduler installed).
    """
    parts = cron.strip().split()
    if len(parts) != 5:
        raise HTTPException(
            status_code=400,
            detail="cron must have 5 fields: 'min hour dom month dow' (e.g. '0 8 * * MON-FRI')",
        )
    for p in parts:
        if not _CRON_FIELD_RE.match(p):
            raise HTTPException(status_code=400, detail=f"invalid cron field: {p!r}")


async def _notify_reload(scope: str, id_: str | None = None) -> None:
    payload = json.dumps({"scope": scope, "id": id_})
    await db.execute("SELECT pg_notify('scheduler_config_reload', $1)", payload)


async def _validate_post_message_params(params: dict) -> None:
    """Validates the params shape for action=post_message. Currently only checks
    that `sender` (optional), if present, is an existing username in
    messaging.users — avoids creating a job that will fail with 400 on every fire."""
    sender = params.get("sender")
    if sender is None or sender == "":
        return
    if not isinstance(sender, str):
        raise HTTPException(status_code=400, detail="sender must be a string")
    row = await db.fetch_one(
        "SELECT 1 FROM messaging.users WHERE username = $1", sender,
    )
    if row is None:
        raise HTTPException(
            status_code=400,
            detail=f"sender {sender!r} does not exist in messaging.users",
        )


async def _attach_runs_info(item: dict) -> None:
    """Attaches `runs: [...]` to a scheduler.custom_jobs item when
    action=post_message — one entry per `<base-topic>-<unix-ts>` conv
    created by the scheduler. Each fire creates a new conv, so this becomes the
    job's run history, exposed to the drawer in the PWA.
    """
    if item.get("action") != "post_message":
        return
    params = item.get("params") or {}
    stream = params.get("stream") if isinstance(params, dict) else None
    base_topic = params.get("topic") if isinstance(params, dict) else None
    if not stream or not base_topic:
        return
    # Regex match to avoid catching topics with a coincident prefix
    # (e.g. base="foo" must not match "foobar-123"). Escape the base to
    # neutralize regex chars the user may have put in the topic.
    escaped = re.escape(base_topic)
    pattern = f"^{escaped}-[0-9]+$"
    rows = await db.fetch_all(
        """SELECT c.id, c.topic_name, c.created_at, c.last_message_at,
                  c.archived_at,
                  (SELECT COUNT(*) FROM messaging.messages m
                    WHERE m.conversation_id = c.id) AS msg_count
             FROM messaging.conversations c
             JOIN messaging.streams s ON s.id = c.stream_id
            WHERE s.name = $1 AND c.topic_name ~ $2
            ORDER BY c.last_message_at DESC
            LIMIT 100""",
        stream, pattern,
    )
    item["runs"] = [
        {
            "id": r["id"],
            "stream": stream,
            "topic": r["topic_name"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "last_message_at": r["last_message_at"].isoformat() if r["last_message_at"] else None,
            "msg_count": r["msg_count"],
            "archived": r["archived_at"] is not None,
        }
        for r in rows
    ]


# ---------- Pydantic models ----------

class CustomJobIn(BaseModel):
    slug: str
    cron: str
    action: str
    params: dict[str, Any] = {}
    description: str | None = None
    enabled: bool = True


class CustomJobPatch(BaseModel):
    cron: str | None = None
    params: dict[str, Any] | None = None
    description: str | None = None
    enabled: bool | None = None


class RoutinePatch(BaseModel):
    cron_override: str | None = None
    enabled: bool | None = None


# ---------- Custom jobs ----------

@router.get("/custom")
async def list_custom_jobs(_: Principal = Depends(get_principal)):
    rows = await db.fetch_all(
        """SELECT slug, cron, action, params, description, enabled,
                  created_by, created_at, updated_at
             FROM scheduler.custom_jobs
            ORDER BY slug"""
    )
    items = []
    for r in rows:
        item = {
            "slug": r["slug"],
            "cron": r["cron"],
            "action": r["action"],
            "params": r["params"] if isinstance(r["params"], dict) else json.loads(r["params"] or "{}"),
            "description": r["description"],
            "enabled": r["enabled"],
            "created_by": r["created_by"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        await _attach_runs_info(item)
        items.append(item)
    return {"items": items}


@router.post("/custom")
async def create_custom_job(
    job: CustomJobIn, principal: Principal = Depends(require_admin)
):
    if not _SLUG_RE.match(job.slug):
        raise HTTPException(status_code=400, detail="slug must be kebab-case")
    if job.action not in _HUMAN_ACTION_WHITELIST:
        raise HTTPException(
            status_code=400,
            detail=f"action {job.action!r} not allowed for custom jobs. "
            f"Allowed: {sorted(_HUMAN_ACTION_WHITELIST)}. Native jobs "
            "(backup/cleanup/cost) are configured at /settings/routines.",
        )
    _validate_cron(job.cron)
    if job.action == "post_message":
        await _validate_post_message_params(job.params or {})
    existing = await db.fetch_one(
        "SELECT slug FROM scheduler.custom_jobs WHERE slug = $1", job.slug
    )
    if existing:
        raise HTTPException(status_code=409, detail=f"slug {job.slug!r} already exists")
    await db.execute(
        """INSERT INTO scheduler.custom_jobs
             (slug, cron, action, params, description, enabled, created_by)
           VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)""",
        job.slug, job.cron, job.action, json.dumps(job.params),
        job.description, job.enabled, principal.username or "user",
    )
    await _notify_reload("custom", job.slug)
    return {"ok": True, "slug": job.slug}


@router.put("/custom/{slug}")
async def update_custom_job(
    slug: str, patch: CustomJobPatch, _: Principal = Depends(require_admin)
):
    existing = await db.fetch_one(
        "SELECT slug, cron, params, enabled FROM scheduler.custom_jobs WHERE slug = $1",
        slug,
    )
    if existing is None:
        raise HTTPException(status_code=404, detail=f"slug {slug!r} does not exist")
    sets: list[str] = []
    args: list[Any] = []
    idx = 1
    if patch.cron is not None:
        _validate_cron(patch.cron)
        sets.append(f"cron = ${idx}"); args.append(patch.cron); idx += 1
    if patch.params is not None:
        # Re-validate params on PATCH (same logic as POST). action can't change
        # via PATCH, so we reuse the action persisted on the existing row.
        existing_action_row = await db.fetch_one(
            "SELECT action FROM scheduler.custom_jobs WHERE slug = $1", slug,
        )
        if existing_action_row and existing_action_row["action"] == "post_message":
            await _validate_post_message_params(patch.params)
        sets.append(f"params = ${idx}::jsonb"); args.append(json.dumps(patch.params)); idx += 1
    if patch.description is not None:
        sets.append(f"description = ${idx}"); args.append(patch.description); idx += 1
    if patch.enabled is not None:
        sets.append(f"enabled = ${idx}"); args.append(patch.enabled); idx += 1
    if not sets:
        return {"ok": True, "slug": slug, "noop": True}
    args.append(slug)
    await db.execute(
        f"UPDATE scheduler.custom_jobs SET {', '.join(sets)} WHERE slug = ${idx}",
        *args,
    )
    await _notify_reload("custom", slug)
    return {"ok": True, "slug": slug}


@router.delete("/custom/{slug}")
async def delete_custom_job(slug: str, _: Principal = Depends(require_admin)):
    row = await db.fetch_one(
        "DELETE FROM scheduler.custom_jobs WHERE slug = $1 RETURNING slug", slug,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"slug {slug!r} does not exist")
    await _notify_reload("custom", slug)
    return {"ok": True, "slug": slug}


# ---------- Native overrides ----------

@router.get("/routines")
async def list_routines(_: Principal = Depends(get_principal)):
    """Returns the framework's native default jobs with the instance
    overrides merged in. Order is set by the defaults YAML."""
    defaults = _load_native_defaults()
    overrides = {
        r["id"]: dict(r)
        for r in await db.fetch_all(
            "SELECT id, cron_override, enabled FROM scheduler.native_overrides"
        )
    }
    items = []
    for job_id, default in defaults.items():
        ov = overrides.get(job_id)
        action = default.get("action") or ""
        params = {k: v for k, v in default.items() if k not in ("id", "cron", "action")}
        items.append({
            "id": job_id,
            "action": action,
            "default_cron": default.get("cron"),
            "cron_override": ov["cron_override"] if ov else None,
            "effective_cron": (ov["cron_override"] if ov and ov["cron_override"] else default.get("cron")),
            "enabled": ov["enabled"] if ov else True,
            "params": params,
            "description": _ACTION_DESCRIPTIONS.get(action) or "",
        })
    return {"items": items}


@router.put("/routines/{job_id}")
async def update_routine(
    job_id: str, patch: RoutinePatch, _: Principal = Depends(require_admin)
):
    defaults = _load_native_defaults()
    if job_id not in defaults:
        raise HTTPException(status_code=404, detail=f"native job {job_id!r} does not exist")
    if patch.cron_override is not None and patch.cron_override.strip():
        _validate_cron(patch.cron_override)
    # Upsert: if there's no row, create it with the patches; if there is, update only
    # the fields passed (NULL stays).
    existing = await db.fetch_one(
        "SELECT id FROM scheduler.native_overrides WHERE id = $1", job_id
    )
    if existing is None:
        cron_ov = patch.cron_override if (patch.cron_override or "").strip() else None
        enabled = patch.enabled if patch.enabled is not None else True
        await db.execute(
            """INSERT INTO scheduler.native_overrides (id, cron_override, enabled)
               VALUES ($1, $2, $3)""",
            job_id, cron_ov, enabled,
        )
    else:
        sets: list[str] = []
        args: list[Any] = []
        idx = 1
        if patch.cron_override is not None:
            cron_ov = patch.cron_override.strip() or None
            sets.append(f"cron_override = ${idx}"); args.append(cron_ov); idx += 1
        if patch.enabled is not None:
            sets.append(f"enabled = ${idx}"); args.append(patch.enabled); idx += 1
        if sets:
            args.append(job_id)
            await db.execute(
                f"UPDATE scheduler.native_overrides SET {', '.join(sets)} WHERE id = ${idx}",
                *args,
            )
    await _notify_reload("native", job_id)
    return {"ok": True, "id": job_id}


@router.delete("/routines/{job_id}")
async def reset_routine(job_id: str, _: Principal = Depends(require_admin)):
    """Removes the override — the native job goes back to the framework default."""
    defaults = _load_native_defaults()
    if job_id not in defaults:
        raise HTTPException(status_code=404, detail=f"native job {job_id!r} does not exist")
    await db.execute(
        "DELETE FROM scheduler.native_overrides WHERE id = $1", job_id,
    )
    await _notify_reload("native", job_id)
    return {"ok": True, "id": job_id}


