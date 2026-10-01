"""KV-backed config de instancia, persistida em web.app_settings.

Single source of truth pra config editavel em runtime via PWA Settings.
Hoje cobre:
  - default_stream: nome do stream default usado pelo PWA.
  - vapid: keypair de push notifications + contact email.

Politica de escrita: idempotente. SET sempre upserta. Generate VAPID
recusa se ja existe (caller passa force=True pra reciclar).
"""
from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization

from . import db


# ---------- low-level KV ----------

async def get(key: str) -> dict | None:
    """Le valor cru da DB (JSONB -> dict). None se ausente."""
    row = await db.fetch_one(
        "SELECT value FROM web.app_settings WHERE key = $1", key,
    )
    if row is None:
        return None
    val = row["value"]
    # asyncpg devolve JSONB como string em alguns paths; normaliza.
    if isinstance(val, str):
        return json.loads(val)
    return dict(val) if val is not None else None


async def set(key: str, value: dict, *, user_id: int | None = None) -> None:
    """Upsert."""
    await db.execute(
        """INSERT INTO web.app_settings (key, value, updated_by)
           VALUES ($1, $2::jsonb, $3)
           ON CONFLICT (key) DO UPDATE SET
             value = EXCLUDED.value,
             updated_at = now(),
             updated_by = EXCLUDED.updated_by""",
        key, json.dumps(value), user_id,
    )


async def delete(key: str) -> None:
    await db.execute("DELETE FROM web.app_settings WHERE key = $1", key)


# ---------- default_stream ----------

async def get_default_stream() -> str:
    """Nome do stream default. "" = sem default (PWA pede pra escolher)."""
    row = await get("default_stream")
    if row and row.get("name"):
        return str(row["name"])
    return ""


async def set_default_stream(name: str, *, user_id: int | None = None) -> None:
    name = (name or "").strip()
    if name:
        await set("default_stream", {"name": name}, user_id=user_id)
    else:
        await delete("default_stream")


# ---------- VAPID ----------

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_vapid_keypair() -> dict[str, str]:
    """EC P-256 keypair em base64url sem padding (formato esperado pelo
    PushManager.subscribe applicationServerKey)."""
    priv = ec.generate_private_key(ec.SECP256R1())
    priv_bytes = priv.private_numbers().private_value.to_bytes(32, "big")
    pub_bytes = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return {
        "public_key": _b64url(pub_bytes),
        "private_key": _b64url(priv_bytes),
    }


async def get_vapid() -> dict | None:
    """Retorna {public_key, private_key, contact_email} ou None. Chaves
    parciais (ex: so public) sao tratadas como ausentes pra evitar config
    quebrada — generate sempre escreve as duas juntas."""
    row = await get("vapid")
    if row and row.get("public_key") and row.get("private_key"):
        return {
            "public_key": str(row["public_key"]),
            "private_key": str(row["private_key"]),
            "contact_email": str(row.get("contact_email") or _default_contact_email()),
        }
    return None


def _default_contact_email() -> str:
    return os.environ.get("ADMIN_EMAIL") or "admin@example.com"


async def set_vapid(
    *,
    public_key: str,
    private_key: str,
    contact_email: str | None = None,
    user_id: int | None = None,
) -> None:
    payload = {
        "public_key": public_key,
        "private_key": private_key,
    }
    if contact_email:
        payload["contact_email"] = contact_email
    await set("vapid", payload, user_id=user_id)


async def set_vapid_contact_email(email: str, *, user_id: int | None = None) -> None:
    """Atualiza so o contact_email mantendo as chaves. No-op se nao tem
    keypair configurado ainda."""
    current = await get("vapid") or {}
    if not (current.get("public_key") and current.get("private_key")):
        return
    current["contact_email"] = email
    await set("vapid", current, user_id=user_id)


async def clear_vapid() -> None:
    await delete("vapid")
