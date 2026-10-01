#!/usr/bin/env python3
"""Applies the versioned SQL migrations in framework/db/migrations/.

Each NNN_name.sql file is applied in a transaction. The version is stored in
public.schema_migrations. Idempotent: rerunning is a no-op if nothing is new.

Usage:
  python3 migrate.py                     # apply pending
  python3 migrate.py --status            # list applied + pending
  python3 migrate.py --baseline <N>      # mark 001..N as applied
                                         # (so already-populated DBs can move
                                         #  to the migration system without
                                         #  re-running what already exists)

The DSN comes from DATABASE_URL. If missing: error.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg


_DEFAULT_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"
MIGRATIONS_DIR = Path(os.environ.get("MIGRATIONS_DIR") or _DEFAULT_DIR)


async def _ensure_table(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS public.schema_migrations (
            version    INT PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def _list_migrations() -> list[tuple[int, str, Path]]:
    """Returns [(version, name, path), ...] sorted by version."""
    out: list[tuple[int, str, Path]] = []
    if not MIGRATIONS_DIR.is_dir():
        return out
    for p in sorted(MIGRATIONS_DIR.glob("*.sql")):
        stem = p.stem  # e.g. "001_init"
        try:
            v_str, name = stem.split("_", 1)
            version = int(v_str)
        except ValueError:
            print(f"ignore (invalid name): {p.name}", file=sys.stderr)
            continue
        out.append((version, name, p))
    return out


async def _applied_versions(conn: asyncpg.Connection) -> set[int]:
    rows = await conn.fetch("SELECT version FROM public.schema_migrations")
    return {r["version"] for r in rows}


async def cmd_apply(dsn: str) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        await _ensure_table(conn)
        applied = await _applied_versions(conn)
        migs = _list_migrations()
        pending = [(v, n, p) for v, n, p in migs if v not in applied]
        if not pending:
            print("migrations up-to-date")
            return 0
        for version, name, path in pending:
            sql = path.read_text(encoding="utf-8")
            print(f"applying {version:03d}_{name}...", end=" ", flush=True)
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO public.schema_migrations (version, name) VALUES ($1, $2)",
                    version,
                    name,
                )
            print("ok")
        print(f"applied {len(pending)} migration(s)")
        return 0
    finally:
        await conn.close()


async def cmd_status(dsn: str) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        await _ensure_table(conn)
        applied = await _applied_versions(conn)
        migs = _list_migrations()
        if not migs:
            print("(no migrations in framework/db/migrations/)")
            return 0
        print(f"{'ver':>4}  {'name':<30}  status")
        print(f"{'-' * 4}  {'-' * 30}  {'-' * 7}")
        for version, name, _ in migs:
            tag = "applied" if version in applied else "pending"
            print(f"{version:>4}  {name:<30}  {tag}")
        pending_count = sum(1 for v, _, _ in migs if v not in applied)
        print()
        print(f"{len(migs)} total, {len(applied & {v for v, _, _ in migs})} applied, {pending_count} pending")
        return 0
    finally:
        await conn.close()


async def cmd_baseline(dsn: str, up_to: int) -> int:
    """Marks every version <= up_to as applied without running the SQL.
    For DBs already populated by hand (old init.sql) joining the system.
    """
    conn = await asyncpg.connect(dsn)
    try:
        await _ensure_table(conn)
        migs = _list_migrations()
        marked = 0
        for version, name, _ in migs:
            if version > up_to:
                break
            r = await conn.execute(
                "INSERT INTO public.schema_migrations (version, name) "
                "VALUES ($1, $2) ON CONFLICT (version) DO NOTHING",
                version,
                name,
            )
            if r.endswith(" 1"):
                print(f"baseline marked: {version:03d}_{name}")
                marked += 1
            else:
                print(f"skip (already applied): {version:03d}_{name}")
        print(f"{marked} marked as baseline (SQL not executed)")
        return 0
    finally:
        await conn.close()


def main() -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("error: DATABASE_URL is not set", file=sys.stderr)
        return 1
    args = sys.argv[1:]
    if not args:
        return asyncio.run(cmd_apply(dsn))
    if args[0] == "--status":
        return asyncio.run(cmd_status(dsn))
    if args[0] == "--baseline":
        if len(args) < 2:
            print("usage: --baseline <version>", file=sys.stderr)
            return 2
        return asyncio.run(cmd_baseline(dsn, int(args[1])))
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
