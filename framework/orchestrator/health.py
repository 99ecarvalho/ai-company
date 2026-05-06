"""Mini servidor HTTP /health pros daemons (reactor, scheduler, watchdog).

Implementacao em http.server da stdlib + thread daemon — zero deps novas,
funciona em codigo sync (watchdog) e em loops asyncio (reactor, scheduler)
sem precisar integrar com o event loop.

Uso:
    from .health import start_health_server

    def status():
        return {"status": "ok", "last_check_at": ..., "stats": {...}}

    start_health_server(port=8811, get_status=status)
    # ... daemon main loop continua
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

GetStatus = Callable[[], dict]


def start_health_server(port: int, get_status: GetStatus) -> HTTPServer:
    """Sobe HTTP server numa thread daemon. Retorna o server (caller pode
    chamar .shutdown() em cleanup; ou nao — daemon thread morre com processo).
    """
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path != "/health":
                self.send_response(404)
                self.end_headers()
                return
            try:
                payload = get_status() or {}
                body = json.dumps(payload).encode()
                code = 200 if payload.get("status") == "ok" else 503
            except Exception as e:
                body = json.dumps({"status": "error", "error": str(e)}).encode()
                code = 500
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args, **kwargs):  # noqa: ARG002
            return  # silencia access log

    server = HTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
        name=f"health-{port}",
    )
    thread.start()
    return server
