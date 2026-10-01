"""Scheduler container. APScheduler.

Jobs come from two sources:
  - native defaults: framework/orchestrator/defaults/schedule.yaml (shipped),
    with per-instance overrides in scheduler.native_overrides (DB).
  - custom jobs: scheduler.custom_jobs (DB), created via PWA / MCP.

Hot-reload via pg_notify('scheduler_config_reload') — no restart.

Supported actions:

  post_message              stream/topic/content (+ optional sender)
  backup_company            retain: <n>  (default BACKUP_RETAIN, 30)
  backup_postgres           retain: <n>  (default BACKUP_RETAIN, 30)
  cleanup_sessions          DELETE web.sessions WHERE expires_at < now()
  cleanup_telemetry         days: <n>  (default 90)
  cleanup_live_events       days: <n>  (default 7)
  reaper_runs               seconds: <n>  (default 600) — running -> stale
  cleanup_runs              days: <n>  (default 7) — DELETE finished rows
  cost_budget_check         posts an alert to budget-alerts when an agent exceeds its cap
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import asyncpg
import requests
import structlog
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger


BROKER_URL = os.environ["BROKER_URL"].rstrip("/")
BROKER_TOKEN = os.environ["BROKER_TOKEN"]
DATABASE_URL = os.environ.get("DATABASE_URL", "")

HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "8811"))

_stats: dict = {
    "started_at": time.time(),
    "jobs_registered": 0,
    "jobs_fired": 0,
    "last_fire_at": None,
    # job_id -> {"last_fire_at": float, "last_status": "ok"|"error", "last_error": str|None,
    #            "last_duration_ms": int}
    "per_job": {},
}

# Module-scoped so the scheduler HTTP server (scheduler_http.py) can
# inspect/modify the runtime: get_jobs(), pause_job(id), resume_job(id), and
# dispatch_job(_jobs_by_id[id]) for "Run now". AsyncIOScheduler is thread-safe
# for these methods — they can be called from the health server thread without touching the loop.
scheduler: AsyncIOScheduler | None = None
_jobs_by_id: dict[str, dict] = {}


# ---------- Logging ----------

logging_level = os.environ.get("LOG_LEVEL", "INFO").upper()
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        getattr(__import__("logging"), logging_level, 20)
    ),
    logger_factory=structlog.PrintLoggerFactory(),
)
structlog.contextvars.bind_contextvars(component="scheduler")
log = structlog.get_logger("scheduler")


_BROKER_HEADERS = {"Authorization": f"Bearer {BROKER_TOKEN}", "Content-Type": "application/json"}


DEFAULTS_PATH = Path(__file__).parent / "defaults" / "schedule.yaml"


def _load_yaml_jobs(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        log.exception("scheduler.config_parse_failed", path=str(path))
        return []
    jobs = data.get("jobs") or []
    return [j for j in jobs if isinstance(j, dict)]


async def _load_native_overrides(pool: asyncpg.Pool | None) -> dict[str, dict]:
    """Returns {id: {cron_override, enabled}} from the native_overrides table."""
    if pool is None:
        return {}
    try:
        rows = await pool.fetch(
            "SELECT id, cron_override, enabled FROM scheduler.native_overrides"
        )
        return {r["id"]: {"cron_override": r["cron_override"], "enabled": r["enabled"]} for r in rows}
    except Exception:
        log.exception("scheduler.native_overrides_fetch_failed")
        return {}


async def _load_custom_jobs(pool: asyncpg.Pool | None) -> list[dict]:
    """Returns a list of job dicts (YAML-compatible shape) from
    scheduler.custom_jobs WHERE enabled=true."""
    if pool is None:
        return []
    try:
        rows = await pool.fetch(
            """SELECT slug, cron, action, params FROM scheduler.custom_jobs
                WHERE enabled = true"""
        )
        out: list[dict] = []
        for r in rows:
            params = r["params"]
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except Exception:
                    params = {}
            if not isinstance(params, dict):
                params = {}
            job = {"id": r["slug"], "cron": r["cron"], "action": r["action"], **params}
            out.append(job)
        return out
    except Exception:
        log.exception("scheduler.custom_jobs_fetch_failed")
        return []


async def load_jobs(pool: asyncpg.Pool | None = None) -> list[dict]:
    """Loads jobs: native defaults (framework YAML) + native overrides (DB)
    + custom jobs (DB).

    Native merge rule: if `native_overrides.enabled=false` → job skipped;
    if `cron_override IS NOT NULL` → use that cron instead of the default.
    """
    defaults = _load_yaml_jobs(DEFAULTS_PATH)
    overrides = await _load_native_overrides(pool)
    custom = await _load_custom_jobs(pool)

    by_id: dict[str, dict] = {}
    for j in defaults:
        jid = j.get("id")
        if not jid:
            continue
        ov = overrides.get(jid)
        if ov and ov.get("enabled") is False:
            continue  # disabled via override
        job = dict(j)
        if ov and ov.get("cron_override"):
            job["cron"] = ov["cron_override"]
        by_id[jid] = job
    for j in custom:
        jid = j.get("id")
        if jid:
            by_id[jid] = j  # DB custom always wins

    if not by_id:
        log.warning("scheduler.no_config", defaults_path=str(DEFAULTS_PATH))
    log.info("scheduler.jobs_loaded",
             defaults=len(defaults), overrides=len(overrides),
             custom=len(custom), total=len(by_id))
    return list(by_id.values())


def run_post_message(job: dict) -> None:
    content = job.get("content", "")
    base_topic = job["topic"]
    # Each fire creates a new conv: topic = `<base>-<unix-ts>`. Lets the PWA
    # list separate runs instead of piling everything into one endless thread.
    fire_topic = f"{base_topic}-{int(time.time())}"
    body: dict = {"stream": job["stream"], "topic": fire_topic, "content": content}
    sender = job.get("sender")
    if sender:
        # Broker resolves by username; without sender, falls back to system-bot.
        body["as_username"] = sender
    try:
        r = requests.post(
            f"{BROKER_URL}/api/messages",
            headers=_BROKER_HEADERS,
            json=body,
            timeout=10,
        )
        ok = r.status_code < 400
        log.info(
            "scheduler.job_fired", job_id=job.get("id"), action="post_message",
            stream=job["stream"], topic=fire_topic, base_topic=base_topic,
            sender=sender or "system-bot", status=r.status_code, ok=ok,
        )
        if not ok:
            log.error("scheduler.job_failed", job_id=job.get("id"), body=r.text[:200])
    except Exception:
        log.exception("scheduler.post_failed", job_id=job.get("id"))


def run_backup_company(job: dict) -> None:
    """Action: builds a tar.gz of company/+agents.yaml+override+notes/.
    Idempotent: writes to /workspace/backups/<timestamp>.tar.gz and applies
    retention (keeps the last BACKUP_RETAIN, default 30).
    """
    import subprocess
    import time
    backup_dir = Path(os.environ.get("BACKUP_DIR", "/workspace/backups"))
    backup_dir.mkdir(parents=True, exist_ok=True)
    retain = int(os.environ.get("BACKUP_RETAIN", job.get("retain", 30)))
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = backup_dir / f"backup-{ts}.tar.gz"
    # Paths relative to the container workspace (bind mounts of instance/).
    paths = [
        "company",
        "agents",
        "memory",
        "docker-compose.override.yml",
    ]
    existing = [p for p in paths if Path("/workspace", p).exists()]
    cmd = ["tar", "-czf", str(out), "-C", "/workspace", *existing]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        size = out.stat().st_size
        log.info("scheduler.backup_ok", job_id=job.get("id"), path=str(out), size_bytes=size)
    except Exception:
        log.exception("scheduler.backup_failed", job_id=job.get("id"))
        return
    # Retention: delete the oldest
    backups = sorted(backup_dir.glob("backup-*.tar.gz"))
    for old in backups[:-retain] if len(backups) > retain else []:
        try:
            old.unlink()
            log.info("scheduler.backup_pruned", path=str(old))
        except Exception:
            log.exception("scheduler.backup_prune_failed", path=str(old))


def run_backup_postgres(job: dict) -> None:
    """Action: pg_dump of the main database -> /workspace/backups/db-<TS>.sql.gz.
    Retention: BACKUP_RETAIN (env, default 30) or job.retain.

    Requires postgresql-client + gzip in the image (see agent.Dockerfile).
    """
    import subprocess
    import time
    backup_dir = Path(os.environ.get("BACKUP_DIR", "/workspace/backups"))
    backup_dir.mkdir(parents=True, exist_ok=True)
    retain = int(os.environ.get("BACKUP_RETAIN", job.get("retain", 30)))
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = backup_dir / f"db-{ts}.sql.gz"
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        log.error("scheduler.db_backup_no_dsn", job_id=job.get("id"))
        return
    # Stream pg_dump | gzip to a file. No shell=True (embedding db_url would be
    # safe — env-controlled — but we avoid injection-by-default).
    try:
        with out.open("wb") as f:
            dump = subprocess.Popen(
                ["pg_dump", "--dbname", db_url, "--no-owner", "--no-acl"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            gz = subprocess.Popen(
                ["gzip", "-c"], stdin=dump.stdout, stdout=f, stderr=subprocess.PIPE,
            )
            dump.stdout.close()
            _, gz_err = gz.communicate()
            _, dump_err = dump.communicate()
            if dump.returncode != 0 or gz.returncode != 0:
                raise RuntimeError(
                    f"pg_dump rc={dump.returncode}, gzip rc={gz.returncode}: "
                    f"{(dump_err or b'').decode()[:300]} {(gz_err or b'').decode()[:300]}"
                )
        size = out.stat().st_size
        log.info("scheduler.db_backup_ok", job_id=job.get("id"), path=str(out), size_bytes=size)
    except Exception:
        log.exception("scheduler.db_backup_failed", job_id=job.get("id"))
        # clean up an empty partial file
        try:
            if out.exists() and out.stat().st_size == 0:
                out.unlink()
        except Exception:
            pass
        return
    # Retention
    backups = sorted(backup_dir.glob("db-*.sql.gz"))
    for old in backups[:-retain] if len(backups) > retain else []:
        try:
            old.unlink()
            log.info("scheduler.db_backup_pruned", path=str(old))
        except Exception:
            log.exception("scheduler.db_backup_prune_failed", path=str(old))


def _pg_notify(channel: str, payload: dict) -> None:
    """Emits pg_notify(channel, payload::jsonb) via psql. Used to signal
    web's SSE that something changed (job fired, paused, etc). The JSON payload
    is escaped by doubling single quotes — SQL-safe because JSON only has
    single quotes inside value strings, never breaking the wrapper."""
    import json as _json
    import subprocess
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        return
    raw = _json.dumps(payload, default=str)
    escaped = raw.replace("'", "''")
    try:
        subprocess.run(
            ["psql", "--dbname", db_url, "-At", "-v", "ON_ERROR_STOP=1", "-c",
             f"SELECT pg_notify('{channel}', '{escaped}');"],
            capture_output=True, text=True, check=True, timeout=5,
        )
    except Exception:
        # best-effort; the scheduler must not fail because of notify
        log.exception("scheduler.pg_notify_failed", channel=channel)


def _run_cleanup_sql(sql: str, label: str, job_id: str | None) -> None:
    """Runs a DELETE wrapped in a CTE + SELECT COUNT(*) to log how many
    went. Uses psql (postgresql-client is already in the image)."""
    import subprocess
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        log.error("scheduler.cleanup_no_dsn", action=label, job_id=job_id)
        return
    try:
        proc = subprocess.run(
            ["psql", "--dbname", db_url, "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
            capture_output=True, text=True, check=True, timeout=60,
        )
        deleted = (proc.stdout or "").strip()
        log.info("scheduler.cleanup_ok", action=label, job_id=job_id, deleted=deleted)
    except subprocess.CalledProcessError as e:
        log.error(
            "scheduler.cleanup_failed", action=label, job_id=job_id,
            rc=e.returncode, stderr=(e.stderr or "")[:300],
        )
    except Exception:
        log.exception("scheduler.cleanup_failed", action=label, job_id=job_id)


def run_cleanup_sessions(job: dict) -> None:
    """DELETE web.sessions with expires_at in the past."""
    _run_cleanup_sql(
        "WITH d AS (DELETE FROM web.sessions WHERE expires_at < now() RETURNING 1) "
        "SELECT COUNT(*) FROM d;",
        "cleanup_sessions", job.get("id"),
    )


def run_cleanup_telemetry(job: dict) -> None:
    """DELETE telemetry.events older than job.days (default 90)."""
    days = max(1, int(job.get("days", 90)))
    _run_cleanup_sql(
        f"WITH d AS (DELETE FROM telemetry.events WHERE ts < now() - INTERVAL '{days} days' RETURNING 1) "
        "SELECT COUNT(*) FROM d;",
        "cleanup_telemetry", job.get("id"),
    )


def run_cleanup_live_events(job: dict) -> None:
    """DELETE telemetry.live_events older than job.days (default 7).
    Grows fast — short TTL."""
    days = max(1, int(job.get("days", 7)))
    _run_cleanup_sql(
        f"WITH d AS (DELETE FROM telemetry.live_events WHERE ts < now() - INTERVAL '{days} days' RETURNING 1) "
        "SELECT COUNT(*) FROM d;",
        "cleanup_live_events", job.get("id"),
    )


def run_reaper_runs(job: dict) -> None:
    """Moves messaging.runs with status='running' and an old heartbeat
    to status='stale'. Migration 030: messaging.runs is the single source of
    truth for CLI state; the trg_notify_runs_stale trigger fires
    pg_notify('conv_activity', kind='run_stale') to update the PWA
    sidebar.

    Default threshold 600s (mirrors web's RUNNER_STUCK_SEC). Covers
    hard SIGKILL, container kill without run_end, a network glitch losing
    the fire-and-forget run_end."""
    seconds = max(60, int(job.get("seconds", 600)))
    _run_cleanup_sql(
        "WITH u AS ("
        "  UPDATE messaging.runs"
        "     SET status = 'stale',"
        "         exit_reason = 'heartbeat_timeout',"
        "         finished_at = now()"
        f"   WHERE status = 'running'"
        f"     AND last_heartbeat_at < now() - INTERVAL '{seconds} seconds'"
        "   RETURNING 1"
        ") SELECT COUNT(*) FROM u;",
        "reaper_runs", job.get("id"),
    )


def run_cleanup_runs(job: dict) -> None:
    """DELETE finished messaging.runs (done/error/stale) older than
    job.days (default 7). Mirrors the telemetry.live_events TTL. Never
    deletes rows with status='running' — the reaper moves them first."""
    days = max(1, int(job.get("days", 7)))
    _run_cleanup_sql(
        "WITH d AS ("
        "  DELETE FROM messaging.runs"
        "   WHERE status IN ('done','error','stale')"
        f"     AND COALESCE(finished_at, started_at) < now() - INTERVAL '{days} days'"
        "   RETURNING 1"
        ") SELECT COUNT(*) FROM d;",
        "cleanup_runs", job.get("id"),
    )


def run_cleanup_live_events_output_full(job: dict) -> None:
    """Removes the `output_full` field from the data JSONB in events > job.hours
    (default 48h). The row stays (5KB inline output + is_error + meta
    preserved); only the large blob goes. "View full" in the PWA only works
    within that window.

    UPDATE instead of DELETE: we don't want to lose the row and break
    metrics (tool_uses count per conv, for example). The returned count is
    how many rows were modified."""
    hours = max(1, int(job.get("hours", 48)))
    _run_cleanup_sql(
        "WITH u AS ("
        "  UPDATE telemetry.live_events "
        "     SET data = data - 'output_full' "
        f"   WHERE ts < now() - INTERVAL '{hours} hours' "
        "     AND data ? 'output_full' "
        "   RETURNING 1"
        ") SELECT COUNT(*) FROM u;",
        "cleanup_live_events_output_full", job.get("id"),
    )


def run_cost_budget_check(job: dict) -> None:
    """Compares today's (UTC) spend per agent vs web.cost_budgets. When it
    exceeds the limit, posts a msg via the broker to #<TERMINAL_NOTIFY_STREAM>/budget-
    alerts and records it in web.budget_alerts to avoid repeating on the same day
    (1-day cooldown per agent).
    """
    import subprocess
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        log.error("scheduler.budget_check_no_dsn", job_id=job.get("id"))
        return
    # Post the alert to the agent's own stream (always exists if it generated cost).
    # Optional override via job.stream to centralize in a specific channel.
    override_stream = job.get("stream")
    sql = (
        "WITH today_spend AS ("
        " SELECT agent, COALESCE(SUM(cost_usd), 0)::float AS spent "
        "   FROM telemetry.events "
        "  WHERE (ts AT TIME ZONE 'UTC')::date = (now() AT TIME ZONE 'UTC')::date "
        "  GROUP BY agent"
        ") "
        "SELECT b.agent, b.daily_usd_limit::float, COALESCE(t.spent, 0)::float "
        "  FROM web.cost_budgets b "
        "  LEFT JOIN today_spend t ON t.agent = b.agent "
        " WHERE COALESCE(t.spent, 0) >= b.daily_usd_limit "
        "   AND NOT EXISTS ("
        "     SELECT 1 FROM web.budget_alerts a "
        "      WHERE a.agent = b.agent "
        "        AND a.alert_date = (now() AT TIME ZONE 'UTC')::date"
        "   );"
    )
    try:
        proc = subprocess.run(
            ["psql", "--dbname", db_url, "-At", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql],
            capture_output=True, text=True, check=True, timeout=30,
        )
    except Exception:
        log.exception("scheduler.budget_check_query_failed", job_id=job.get("id"))
        return
    lines = [l for l in (proc.stdout or "").strip().splitlines() if l]
    if not lines:
        log.info("scheduler.budget_check_ok", over=0, job_id=job.get("id"))
        return
    fired = 0
    for line in lines:
        parts = line.split("|")
        if len(parts) != 3:
            continue
        agent, lim_s, spent_s = parts
        try:
            lim = float(lim_s)
            spent = float(spent_s)
        except ValueError:
            continue
        msg = (
            f"⚠️ **Budget exceeded**\n\n"
            f"Agent `{agent}` spent **${spent:.4f}** today "
            f"(limit ${lim:.4f}, {spent / lim * 100:.0f}%).\n\n"
            f"Consider pausing this agent's work or adjusting the budget."
        )
        target_stream = override_stream or agent
        try:
            r = requests.post(
                f"{BROKER_URL}/api/messages",
                headers=_BROKER_HEADERS,
                json={"stream": target_stream, "topic": "budget-alerts", "content": msg},
                timeout=10,
            )
            if r.status_code >= 400:
                log.error(
                    "scheduler.budget_post_failed", agent=agent,
                    status=r.status_code, body=r.text[:200],
                )
                continue
        except Exception:
            log.exception("scheduler.budget_post_failed", agent=agent)
            continue
        # cooldown: record the alert
        try:
            subprocess.run(
                ["psql", "--dbname", db_url, "-At", "-v", "ON_ERROR_STOP=1", "-c",
                 "INSERT INTO web.budget_alerts (agent, alert_date, spent_usd) "
                 f"VALUES ('{agent}', (now() AT TIME ZONE 'UTC')::date, {spent}) "
                 "ON CONFLICT (agent, alert_date) DO NOTHING;"],
                capture_output=True, text=True, check=True, timeout=10,
            )
        except Exception:
            log.exception("scheduler.budget_alert_insert_failed", agent=agent)
        fired += 1
        log.warning("scheduler.budget_alert_sent", agent=agent, spent=spent, lim=lim)
    log.info("scheduler.budget_check_ok", over=fired, job_id=job.get("id"))


def dispatch_job(job: dict) -> None:
    action = job.get("action")
    job_id = job.get("id") or "<no-id>"
    started = time.time()
    _stats["jobs_fired"] += 1
    _stats["last_fire_at"] = started
    status = "ok"
    err: str | None = None
    try:
        if action == "post_message":
            run_post_message(job)
        elif action == "backup_company":
            run_backup_company(job)
        elif action == "backup_postgres":
            run_backup_postgres(job)
        elif action == "cleanup_sessions":
            run_cleanup_sessions(job)
        elif action == "cleanup_telemetry":
            run_cleanup_telemetry(job)
        elif action == "cleanup_live_events":
            run_cleanup_live_events(job)
        elif action == "cleanup_live_events_output_full":
            run_cleanup_live_events_output_full(job)
        elif action == "reaper_runs":
            run_reaper_runs(job)
        elif action == "cleanup_runs":
            run_cleanup_runs(job)
        elif action == "cost_budget_check":
            run_cost_budget_check(job)
        else:
            log.error("scheduler.unknown_action", action=action, job_id=job_id)
            status = "error"
            err = f"unknown action {action!r}"
    except Exception as e:
        log.exception("scheduler.dispatch_failed", job_id=job_id)
        status = "error"
        err = f"{type(e).__name__}: {e}"[:300]
    finally:
        duration_ms = int((time.time() - started) * 1000)
        _stats["per_job"][job_id] = {
            "last_fire_at": started,
            "last_status": status,
            "last_error": err,
            "last_duration_ms": duration_ms,
        }
        # Notify SSE (/api/scheduler/events) — best-effort, non-blocking.
        _pg_notify("scheduler_event", {
            "kind": "job_fired",
            "job_id": job_id,
            "action": action,
            "status": status,
            "last_fire_at": started,
            "last_duration_ms": duration_ms,
        })


def _health_status() -> dict:
    return {
        "status": "ok",
        "uptime_sec": int(time.time() - _stats["started_at"]),
        "jobs_registered": _stats["jobs_registered"],
        "jobs_fired": _stats["jobs_fired"],
        "last_fire_at": _stats["last_fire_at"],
    }


def _register_job(job: dict) -> bool:
    """Registers (or re-registers) a job in the APScheduler scheduler. Returns
    True if ok, False if invalid. Idempotent via replace_existing."""
    job_id = job.get("id")
    cron = job.get("cron")
    if not job_id or not cron or not scheduler:
        log.warning("scheduler.invalid_job", job=job)
        return False
    try:
        trigger = CronTrigger.from_crontab(cron, timezone=os.environ.get("TZ", "UTC"))
        scheduler.add_job(
            dispatch_job, trigger, args=[job], id=job_id, replace_existing=True
        )
        _jobs_by_id[job_id] = job
        log.info("scheduler.registered", job_id=job_id, cron=cron, action=job.get("action"))
        return True
    except Exception:
        log.exception("scheduler.register_failed", job_id=job_id)
        return False


def _unregister_job(job_id: str) -> None:
    if not scheduler:
        return
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass
    _jobs_by_id.pop(job_id, None)
    log.info("scheduler.unregistered", job_id=job_id)


async def _handle_reload(pool: asyncpg.Pool, scope: str, id_: str | None) -> None:
    """Applies a targeted or full reload to the scheduler at runtime."""
    log.info("scheduler.config_reload_received", scope=scope, id=id_)
    if scope == "all" or id_ is None:
        jobs = await load_jobs(pool)
        desired_ids = {j["id"] for j in jobs if j.get("id")}
        for stale_id in list(_jobs_by_id.keys()):
            if stale_id not in desired_ids:
                _unregister_job(stale_id)
        for job in jobs:
            _register_job(job)
        return

    if scope == "custom":
        row = await pool.fetchrow(
            """SELECT slug, cron, action, params, enabled
                 FROM scheduler.custom_jobs WHERE slug = $1""",
            id_,
        )
        if row is None or not row["enabled"]:
            _unregister_job(id_)
            return
        params = row["params"]
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except Exception:
                params = {}
        if not isinstance(params, dict):
            params = {}
        job = {"id": row["slug"], "cron": row["cron"], "action": row["action"], **params}
        _register_job(job)
        return

    if scope == "native":
        defaults = {j.get("id"): j for j in _load_yaml_jobs(DEFAULTS_PATH) if j.get("id")}
        if id_ not in defaults:
            return
        ov_row = await pool.fetchrow(
            "SELECT cron_override, enabled FROM scheduler.native_overrides WHERE id = $1",
            id_,
        )
        if ov_row and ov_row["enabled"] is False:
            _unregister_job(id_)
            return
        job = dict(defaults[id_])
        if ov_row and ov_row["cron_override"]:
            job["cron"] = ov_row["cron_override"]
        _register_job(job)
        return

    log.warning("scheduler.unknown_reload_scope", scope=scope)


async def _config_reload_loop() -> None:
    """Dedicated conn for LISTEN scheduler_config_reload. Reconnects with
    backoff if it drops. Pattern copied from web/app/broker.py:314-382."""
    if not DATABASE_URL:
        log.warning("scheduler.reload_loop_disabled", reason="no DATABASE_URL")
        return
    backoff = 1
    while True:
        try:
            conn = await asyncpg.connect(DATABASE_URL)
            pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
            queue: asyncio.Queue[str] = asyncio.Queue()

            def _cb(_c, _pid, _chan, payload):
                queue.put_nowait(payload or "{}")

            await conn.add_listener("scheduler_config_reload", _cb)
            log.info("scheduler.reload_loop_started")
            backoff = 1
            try:
                while True:
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=30.0)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        data = json.loads(payload)
                    except Exception:
                        log.warning("scheduler.reload_payload_invalid", payload=payload[:200])
                        continue
                    scope = data.get("scope") or "all"
                    id_ = data.get("id")
                    await _handle_reload(pool, scope, id_)
            finally:
                try:
                    await conn.remove_listener("scheduler_config_reload", _cb)
                except Exception:
                    pass
                await conn.close()
                await pool.close()
        except Exception:
            log.exception("scheduler.reload_loop_crashed")
            await asyncio.sleep(min(backoff, 30))
            backoff = min(backoff * 2, 30)


async def run() -> None:
    global scheduler
    # Scheduler HTTP server: besides /health, exposes /jobs, /jobs/{id}/run,
    # /jobs/{id}/pause, /jobs/{id}/resume — consumed by the broker, which proxies
    # them to the PWA UI. Runs in a daemon thread (http.server, sync).
    import sys
    from . import scheduler_http
    # Pass this module instance (which may be __main__ when run as
    # `python -m orchestrator.scheduler`) so scheduler_http doesn't read
    # a second imported copy with empty state.
    scheduler_http.start_server(HEALTH_PORT, _health_status, sys.modules[__name__])

    scheduler = AsyncIOScheduler(timezone=os.environ.get("TZ", "UTC"))

    # asyncpg pool for load_jobs (reads native_overrides + custom_jobs).
    load_pool: asyncpg.Pool | None = None
    if DATABASE_URL:
        try:
            load_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
        except Exception:
            log.exception("scheduler.db_pool_failed — defaults-only fallback")

    jobs = await load_jobs(load_pool)
    if load_pool is not None:
        await load_pool.close()  # _config_reload_loop creates its own pool
    log.info("scheduler.loading", num_jobs=len(jobs), health_port=HEALTH_PORT)

    for job in jobs:
        if _register_job(job):
            _stats["jobs_registered"] += 1

    scheduler.start()
    log.info("scheduler.started")

    # Hot-reload task in parallel — listens to pg_notify and reschedules without a restart.
    reload_task = asyncio.create_task(_config_reload_loop())

    try:
        stop_event = asyncio.Event()
        await stop_event.wait()
    finally:
        reload_task.cancel()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("scheduler.shutdown")


if __name__ == "__main__":
    main()
