"""Auth helpers: Bearer token | session cookie -> user record.

Tres tipos de principal:
  1. Bot (agent): api_token em messaging.users (kind='bot').
  2. Service daemon (reactor, scheduler): token fixo do .env
     (ORCHESTRATOR_TOKEN, SCHEDULER_TOKEN). Tratado como user virtual.
  3. Humano: session cookie 'agf_session' assinado pelo /api/auth/login
     (bcrypt check + INSERT em web.sessions).

DEV BYPASS: se WEB_AUTH_DEV_BYPASS=1 e nenhum token/cookie veio, retorna
admin implicito (single-user local). Em producao manter unset.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from . import db


SERVICE_TOKENS: dict[str, str] = {}   # token -> service_name
ADMIN_EMAIL: str = "admin@example.com"
SESSION_COOKIE = "agf_session"
SESSION_TTL_DAYS = 7


def init_service_tokens() -> None:
    """Le tokens fixos de service daemons do env. Chamado no startup."""
    global SERVICE_TOKENS, ADMIN_EMAIL
    SERVICE_TOKENS = {}
    if tok := os.environ.get("ORCHESTRATOR_TOKEN"):
        SERVICE_TOKENS[tok] = "orchestrator"
    if tok := os.environ.get("SCHEDULER_TOKEN"):
        SERVICE_TOKENS[tok] = "scheduler"
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")


def _dev_bypass_env() -> bool:
    return os.environ.get("WEB_AUTH_DEV_BYPASS", "").strip() in ("1", "true", "yes")


async def _dev_bypass() -> bool:
    """Hot-readable: DB tem precedencia sobre env. Quando user define
    senha via /onboard ou /api/auth/set-password, gravamos
    {"enabled": false} em web.app_settings -> proxima request ja
    exige login, sem restart. Env var ainda funciona como bootstrap
    inicial (compose .env: WEB_AUTH_DEV_BYPASS=1)."""
    from . import app_settings as _s
    row = await _s.get("auth_dev_bypass")
    if row is not None:
        return bool(row.get("enabled"))
    return _dev_bypass_env()


def cookie_secure() -> bool:
    """Retorna True se cookies devem ser marcados Secure (=requer HTTPS)."""
    return os.environ.get("WEB_COOKIE_SECURE", "").strip() in ("1", "true", "yes")


@dataclass
class Principal:
    """Quem esta fazendo a requisicao."""
    user_id: int | None       # row em messaging.users (None pra service tokens puros)
    username: str
    kind: str                 # 'human' | 'bot' | 'service'
    is_admin: bool = False


def _extract_bearer(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth.removeprefix("Bearer ").strip()
    return None


# ---------- Sessions (humans via cookie) ----------

async def create_session(user_id: int, user_agent: str | None = None) -> tuple[str, datetime]:
    """Cria uma session pra um user humano. Retorna (token, expires_at)."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(tz=timezone.utc) + timedelta(days=SESSION_TTL_DAYS)
    await db.execute(
        """INSERT INTO web.sessions (token, user_id, expires_at, user_agent)
           VALUES ($1, $2, $3, $4)""",
        token, user_id, expires_at, user_agent,
    )
    return token, expires_at


async def resolve_session(token: str) -> Principal | None:
    """Resolve cookie -> Principal. Retorna None se invalido/expirado."""
    row = await db.fetch_one(
        """SELECT u.id, u.username, u.kind, u.is_admin, s.expires_at
             FROM web.sessions s
             JOIN messaging.users u ON u.id = s.user_id
            WHERE s.token = $1 AND s.expires_at > now()""",
        token,
    )
    if row is None:
        return None
    return Principal(
        user_id=row["id"],
        username=row["username"],
        kind=row["kind"],
        is_admin=row["is_admin"],
    )


async def delete_session(token: str) -> None:
    await db.execute("DELETE FROM web.sessions WHERE token = $1", token)


async def cleanup_expired_sessions() -> int:
    """Apaga sessions expiradas. Pode ser chamado on-the-fly."""
    result = await db.execute("DELETE FROM web.sessions WHERE expires_at <= now()")
    # asyncpg.execute retorna 'DELETE <n>'
    try:
        return int(str(result).split()[-1])
    except Exception:
        return 0


# ---------- get_principal ----------

async def get_principal(request: Request) -> Principal:
    """Resolve credenciais -> Principal.

    Ordem: (1) Bearer service token, (2) Bearer bot api_token, (3) cookie de
    session, (4) DEV_BYPASS=admin implicito, (5) 401.
    """
    token = _extract_bearer(request)

    # Service daemon (reactor/scheduler)
    if token and token in SERVICE_TOKENS:
        return Principal(user_id=None, username=SERVICE_TOKENS[token], kind="service", is_admin=True)

    # Bot (agent)
    if token:
        row = await db.fetch_one(
            "SELECT id, username, kind, is_admin FROM messaging.users WHERE api_token = $1",
            token,
        )
        if row is not None:
            return Principal(user_id=row["id"], username=row["username"], kind=row["kind"], is_admin=row["is_admin"])
        raise HTTPException(status_code=401, detail="invalid bearer token")

    # Session cookie (humano logado pelo PWA)
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        principal = await resolve_session(cookie)
        if principal is not None:
            return principal
        # Cookie invalido/expirado: cai pro fallback (que pode ser 401).

    # Dev mode local: sem credenciais -> admin implicito.
    if await _dev_bypass():
        row = await db.fetch_one(
            "SELECT id, username, kind, is_admin FROM messaging.users WHERE email = $1 AND kind = 'human'",
            ADMIN_EMAIL,
        )
        if row is not None:
            return Principal(user_id=row["id"], username=row["username"], kind=row["kind"], is_admin=row["is_admin"])

    raise HTTPException(status_code=401, detail="not authenticated")


async def require_admin(request: Request) -> Principal:
    p = await get_principal(request)
    if not p.is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    return p
