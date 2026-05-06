"""Backfill idempotente: tasks em company/tasks/<slug>/metadata.yaml → Postgres.

Roda uma vez na instancia apos aplicar a migration 006. Le cada metadata.yaml
de company/tasks/ (inclui _archive/ como archived_at=created_at), insere a
linha em tasks.tasks + phases + worktrees.

Execucao:
    docker compose exec -T web python3 /app/framework/scripts/migrate_tasks_fs_to_db.py

Ou via make:
    make tasks-migrate

Idempotente: ON CONFLICT DO UPDATE mantem o que ja foi backfillado.
Nao deleta arquivos — operador remove manualmente quando validar que os
dados estao OK no banco.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import yaml


def _parse_iso(val):
    if val is None:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    s = str(val).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


async def backfill_dir(conn: asyncpg.Connection, base: Path, archived: bool) -> int:
    count = 0
    if not base.is_dir():
        return 0
    for task_dir in sorted(base.iterdir()):
        if not task_dir.is_dir() or task_dir.name.startswith("_"):
            continue
        meta_path = task_dir / "metadata.yaml"
        if not meta_path.exists():
            continue
        try:
            meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            print(f"  skip (yaml invalido) {task_dir.name}: {e}", file=sys.stderr)
            continue
        if not isinstance(meta, dict):
            continue
        slug = meta.get("slug") or task_dir.name
        title = meta.get("title") or slug
        workflow = meta.get("workflow")
        status = meta.get("status") or "in_progress"
        # Normaliza status que saiu de filesystem.
        if status not in ("in_progress", "done", "blocked", "human_review"):
            status = "in_progress"
        current_step = None if archived else meta.get("current_step")
        current_agent = None if archived else meta.get("current_agent")
        created_at = _parse_iso(meta.get("created_at")) or datetime.now(timezone.utc)
        updated_at = _parse_iso(meta.get("updated_at")) or created_at
        archived_at = updated_at if archived else None

        # Campos nao-schema vao pra metadata_extra.
        extra = {
            k: v for k, v in meta.items()
            if k not in (
                "slug", "title", "workflow", "status", "current_step",
                "current_agent", "origin_stream", "origin_topic",
                "complexity", "impact", "difficulty", "origin",
                "blocked_reason", "created_at", "updated_at", "phases",
                "worktrees", "assigned_topic", "__started_at",
            )
        }
        # assigned_topic/__started_at sao dinamicos; nao persiste.

        task_id = await conn.fetchval(
            """INSERT INTO tasks.tasks
                (slug, title, workflow, status, current_step, current_agent,
                 complexity, impact, difficulty, origin,
                 origin_stream, origin_topic, blocked_reason,
                 metadata_extra, archived_at, created_at, updated_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb,$15,$16,$17)
               ON CONFLICT (slug) DO UPDATE SET
                 title = EXCLUDED.title,
                 workflow = COALESCE(tasks.tasks.workflow, EXCLUDED.workflow),
                 status = EXCLUDED.status,
                 current_step = EXCLUDED.current_step,
                 current_agent = EXCLUDED.current_agent,
                 complexity = COALESCE(tasks.tasks.complexity, EXCLUDED.complexity),
                 impact = COALESCE(tasks.tasks.impact, EXCLUDED.impact),
                 difficulty = COALESCE(tasks.tasks.difficulty, EXCLUDED.difficulty),
                 origin = COALESCE(tasks.tasks.origin, EXCLUDED.origin),
                 origin_stream = COALESCE(tasks.tasks.origin_stream, EXCLUDED.origin_stream),
                 origin_topic = COALESCE(tasks.tasks.origin_topic, EXCLUDED.origin_topic),
                 blocked_reason = EXCLUDED.blocked_reason,
                 metadata_extra = EXCLUDED.metadata_extra,
                 archived_at = EXCLUDED.archived_at,
                 updated_at = EXCLUDED.updated_at
               RETURNING id""",
            slug, title, workflow, status, current_step, current_agent,
            meta.get("complexity"), meta.get("impact"), meta.get("difficulty"),
            meta.get("origin"),
            meta.get("origin_stream"), meta.get("origin_topic"),
            meta.get("blocked_reason"),
            json.dumps(extra), archived_at, created_at, updated_at,
        )

        # Phases. Limpa existentes e re-popula — idempotente sem checagem fina.
        await conn.execute("DELETE FROM tasks.phases WHERE task_id = $1", task_id)
        for idx, p in enumerate(meta.get("phases") or []):
            if not isinstance(p, dict):
                continue
            await conn.execute(
                """INSERT INTO tasks.phases
                    (task_id, idx, step, agent, started_at, completed_at, artifact, summary)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
                task_id, idx,
                str(p.get("step") or p.get("name") or f"step{idx}"),
                p.get("agent") or p.get("by"),
                _parse_iso(p.get("started_at")),
                _parse_iso(p.get("completed_at") or p.get("done_at")),
                p.get("artifact"),
                p.get("summary"),
            )

        # Worktrees.
        wts = meta.get("worktrees")
        if isinstance(wts, dict):
            # idempotente: limpa e re-insere
            await conn.execute("DELETE FROM tasks.worktrees WHERE task_id = $1", task_id)
            for repo, info in wts.items():
                if not isinstance(info, dict):
                    continue
                branch = info.get("branch") or "unknown"
                path = info.get("path")
                try:
                    await conn.execute(
                        """INSERT INTO tasks.worktrees (task_id, repo, branch, path)
                           VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING""",
                        task_id, repo, branch, path,
                    )
                except Exception:
                    pass

        print(f"  migrated: {slug} (archived={archived}, phases={len(meta.get('phases') or [])})")
        count += 1
    return count


async def main():
    # Paths default dentro do container web.
    tasks_dir = Path(os.environ.get("TASKS_DIR", "/workspace/company/tasks"))
    archive_dir = tasks_dir / "_archive"
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("ERRO: DATABASE_URL nao setado", file=sys.stderr)
        sys.exit(1)
    if not tasks_dir.is_dir():
        print(f"Nenhuma pasta em {tasks_dir} — nada pra migrar.")
        return

    conn = await asyncpg.connect(dsn)
    try:
        active = await backfill_dir(conn, tasks_dir, archived=False)
        archived = await backfill_dir(conn, archive_dir, archived=True)
        print(f"OK. {active} task(s) ativa(s), {archived} arquivada(s) migrada(s).")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
