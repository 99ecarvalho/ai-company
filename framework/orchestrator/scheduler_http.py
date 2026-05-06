"""HTTP server do scheduler — expoe /health (compat) + endpoints de inspecao
e controle runtime usados pelo PWA.

Roda em thread daemon com http.server (mesmo padrao do health.py generico),
mas precisa do estado do scheduler.py (scheduler global, _stats, _jobs_by_id),
por isso vive aqui — nao no health.py compartilhado pelos outros daemons.

Endpoints (sem auth — exposto so na rede compose; broker no `web` proxia
com auth):

  GET  /health                  — payload do _health_status (compat com healthcheck)
  GET  /jobs                    — lista jobs registrados + next_run_time + last fire
  POST /jobs/{id}/run           — dispara dispatch_job(job) em thread; 202 imediato
  POST /jobs/{id}/pause         — scheduler.pause_job(id); 204
  POST /jobs/{id}/resume        — scheduler.resume_job(id); 204

Pause/resume sao runtime-only — somem em restart (schedule.yaml eh source of
truth). Tooltip na UI deve avisar.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

import structlog


log = structlog.get_logger("scheduler-http")

GetStatus = Callable[[], dict]


def _job_to_dict(apjob, job_def: dict | None, per_job_stats: dict) -> dict:
    """Serializa um Job do APScheduler + metadata original do yaml + ultima fire."""
    next_run = apjob.next_run_time.isoformat() if apjob.next_run_time else None
    paused = apjob.next_run_time is None
    last = per_job_stats.get(apjob.id) or {}
    return {
        "id": apjob.id,
        "action": (job_def or {}).get("action"),
        "cron": (job_def or {}).get("cron"),
        "params": {k: v for k, v in (job_def or {}).items() if k not in ("id", "cron", "action")},
        "next_run_time": next_run,
        "paused": paused,
        "last_fire_at": last.get("last_fire_at"),
        "last_status": last.get("last_status"),
        "last_error": last.get("last_error"),
        "last_duration_ms": last.get("last_duration_ms"),
    }


def start_server(port: int, get_status: GetStatus, scheduler_mod) -> HTTPServer:
    """Sobe o servidor HTTP em thread daemon. Retorna o server.

    `scheduler_mod` precisa ser o objeto modulo que **executa** o daemon (ex.:
    `sys.modules[__name__]` chamado de dentro de scheduler.py). Importa-lo aqui
    com `from . import scheduler` cria uma SEGUNDA instancia do modulo quando o
    daemon roda como `python -m orchestrator.scheduler` (ai o modulo principal
    fica como `__main__` e nao como `orchestrator.scheduler`) — handlers leem
    estado vazio. Passar via parametro garante a mesma instancia.
    """

    class Handler(BaseHTTPRequestHandler):
        def _write_json(self, code: int, payload: dict | list) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _write_empty(self, code: int) -> None:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _not_found(self) -> None:
            self._write_json(404, {"error": "not found"})

        def log_message(self, *args, **kwargs):  # noqa: ARG002, N802
            return  # silencia access log padrao

        # ---------- GET ----------

        def do_GET(self):  # noqa: N802
            path = self.path.rstrip("/") or "/"

            if path == "/health":
                try:
                    payload = get_status() or {}
                    code = 200 if payload.get("status") == "ok" else 503
                except Exception as e:
                    self._write_json(500, {"status": "error", "error": str(e)})
                    return
                self._write_json(code, payload)
                return

            if path == "/jobs":
                sched = scheduler_mod.scheduler
                if sched is None:
                    self._write_json(503, {"error": "scheduler not started"})
                    return
                per_job = scheduler_mod._stats.get("per_job", {})
                items = []
                for apjob in sched.get_jobs():
                    job_def = scheduler_mod._jobs_by_id.get(apjob.id)
                    items.append(_job_to_dict(apjob, job_def, per_job))
                self._write_json(200, {"jobs": items})
                return

            self._not_found()

        # ---------- POST ----------

        def do_POST(self):  # noqa: N802
            path = self.path.rstrip("/") or "/"
            sched = scheduler_mod.scheduler
            if sched is None:
                self._write_json(503, {"error": "scheduler not started"})
                return

            # /jobs/{id}/{run|pause|resume}
            if path.startswith("/jobs/"):
                parts = path[len("/jobs/"):].split("/")
                if len(parts) == 2:
                    job_id, action = parts
                    if action == "run":
                        return self._run_now(job_id)
                    if action == "pause":
                        return self._pause(sched, job_id)
                    if action == "resume":
                        return self._resume(sched, job_id)

            self._not_found()

        def _run_now(self, job_id: str) -> None:
            job_def = scheduler_mod._jobs_by_id.get(job_id)
            if job_def is None:
                self._write_json(404, {"error": f"job {job_id!r} nao registrado"})
                return
            # dispatch_job eh sync (usa requests/subprocess/psql). Roda em thread
            # propria pra responder 202 sem bloquear (backups demoram).
            t = threading.Thread(
                target=scheduler_mod.dispatch_job,
                args=(job_def,),
                daemon=True,
                name=f"run-now-{job_id}",
            )
            t.start()
            log.info("scheduler.run_now", job_id=job_id, action=job_def.get("action"))
            self._write_json(202, {"ok": True, "job_id": job_id, "triggered": True})

        def _pause(self, sched, job_id: str) -> None:
            try:
                sched.pause_job(job_id)
            except Exception as e:
                self._write_json(404, {"error": str(e)})
                return
            log.info("scheduler.paused", job_id=job_id)
            self._write_empty(204)

        def _resume(self, sched, job_id: str) -> None:
            try:
                sched.resume_job(job_id)
            except Exception as e:
                self._write_json(404, {"error": str(e)})
                return
            log.info("scheduler.resumed", job_id=job_id)
            self._write_empty(204)

    server = HTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
        name=f"scheduler-http-{port}",
    )
    thread.start()
    log.info("scheduler_http.started", port=port)
    return server
