"""Disparo de web push via pywebpush, usando Postgres como backing store."""
from __future__ import annotations

import asyncio
import json

import structlog
from pywebpush import WebPushException, webpush

from . import db


log = structlog.get_logger("push_dispatcher")


class PushDispatcher:
    def __init__(self, *, vapid_private_key: str, vapid_claims_sub: str):
        self._vapid_private_key = vapid_private_key
        # vapid_claims_sub pode vir com ou sem mailto:
        sub = vapid_claims_sub if vapid_claims_sub.startswith("mailto:") else f"mailto:{vapid_claims_sub}"
        self._vapid_claims = {"sub": sub}

    def _send_one_sync(self, endpoint: str, p256dh: str, auth: str, payload: str) -> tuple[str, bool, str]:
        subscription_info = {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}
        try:
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=self._vapid_private_key,
                vapid_claims=dict(self._vapid_claims),
                timeout=10,
            )
            return endpoint, True, "ok"
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                return endpoint, False, f"pruned ({status})"
            return endpoint, False, f"{e}"
        except Exception as e:
            return endpoint, False, f"unexpected: {e}"

    async def send_one(self, endpoint: str, p256dh: str, auth: str,
                       title: str, body: str, url: str | None = None, tag: str | None = None) -> bool:
        payload = json.dumps({
            "title": title,
            "body": body,
            "data": {"url": url or "/"},
            "tag": tag or "default",
        })
        _, ok, msg = await asyncio.to_thread(self._send_one_sync, endpoint, p256dh, auth, payload)
        if not ok and msg.startswith("pruned"):
            await db.execute("DELETE FROM web.push_subscriptions WHERE endpoint = $1", endpoint)
        return ok

    async def broadcast(self, *, title: str, body: str, url: str | None = None, tag: str | None = None) -> dict:
        subs = await db.fetch_all("SELECT endpoint, p256dh, auth FROM web.push_subscriptions")
        if not subs:
            return {"sent": 0, "failed": 0, "total": 0}
        payload = json.dumps({
            "title": title,
            "body": body,
            "data": {"url": url or "/"},
            "tag": tag or "default",
        })
        results = await asyncio.gather(*[
            asyncio.to_thread(self._send_one_sync, s["endpoint"], s["p256dh"], s["auth"], payload)
            for s in subs
        ])
        sent = sum(1 for _, ok, _ in results if ok)
        failed = sum(1 for _, ok, _ in results if not ok)
        # prune subscriptions invalidas
        for ep, ok, msg in results:
            if not ok and msg.startswith("pruned"):
                await db.execute("DELETE FROM web.push_subscriptions WHERE endpoint = $1", ep)
        log.info("push.broadcast", total=len(subs), sent=sent, failed=failed)
        return {"sent": sent, "failed": failed, "total": len(subs)}
