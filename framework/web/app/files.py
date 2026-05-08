"""File viewer + upload endpoints.

Regras:
  - Bases permitidas: company (rw), repos (ro), agents (rw), sessions (ro).
  - Path traversal (..) bloqueado.
  - Upload salva em /workspace/company/uploads/<yyyymmdd>/<filename>.
  - Viewer detecta mime por extensao; retorna bytes + content-type.
"""
from __future__ import annotations

import mimetypes
import re
from datetime import datetime, timezone
from os import walk
from pathlib import Path
from typing import Iterable

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse

from .auth import Principal, get_principal


router = APIRouter(prefix="/api/files")

COMPANY_DIR = Path("/workspace/company")
REPOS_DIR = Path("/workspace/repos")
AGENTS_DIR = Path("/workspace/agents")
SESSIONS_DIR = Path("/workspace/sessions")
UPLOADS_SUBDIR = "uploads"

# Mapa prefix-relativo -> base no FS. Se o frontend mandar caminhos com
# prefixo /app/<x>/ ou /workspace/<x>/, ele normaliza antes; aqui esperamos
# sempre o shape relativo "<base>/...".
_BASES = {
    "company": COMPANY_DIR,
    "repos": REPOS_DIR,
    "agents": AGENTS_DIR,
    "sessions": SESSIONS_DIR,
}

# Max upload 50 MB
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# Safe filename: alnum, dot, dash, underscore (no slashes, no parents)
SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _resolve(rel: str) -> Path:
    """Resolve 'company/x', 'repos/x', 'agents/x' ou 'sessions/x' para Path
    absoluto dentro das bases permitidas. Bloqueia traversal.
    """
    rel = rel.lstrip("/")
    parts = rel.split("/", 1)
    head = parts[0]
    sub = parts[1] if len(parts) > 1 else ""
    base = _BASES.get(head)
    if base is None:
        raise HTTPException(
            status_code=400,
            detail="path must start with company/, repos/, agents/ or sessions/",
        )
    full = (base / sub).resolve()
    try:
        full.relative_to(base.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="path outside allowed base")
    return full


def _list_dir(path: Path, rel_prefix: str, show_hidden: bool = False) -> list[dict]:
    out = []
    for entry in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if not show_hidden and entry.name.startswith(".") and entry.name != ".gitkeep":
            continue
        rel = f"{rel_prefix}/{entry.name}".strip("/")
        is_dir = entry.is_dir()
        size = None if is_dir else entry.stat().st_size
        out.append({
            "name": entry.name,
            "path": rel,
            "is_dir": is_dir,
            "size": size,
        })
    return out


@router.get("/list")
async def list_files(
    path: str = "company",
    show_hidden: bool = False,
    _: Principal = Depends(get_principal),
):
    """List directory contents under company/, repos/ ou agents/.
    show_hidden=true inclui dotfiles (default oculta — repo `.git/` etc.
    polui rapido)."""
    full = _resolve(path)
    if not full.exists():
        raise HTTPException(status_code=404, detail="path does not exist")
    if not full.is_dir():
        raise HTTPException(status_code=400, detail="not a directory")
    entries = _list_dir(full, path, show_hidden=show_hidden)
    return {"path": path, "entries": entries}


# Diretorios que quase sempre sao ruido em busca de arquivo. Skipados no walk
# pra nao estourar limit em node_modules/.git/etc. Match exato por nome.
_SEARCH_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".svelte-kit", ".turbo", ".parcel-cache",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "coverage",
    # Go modules / PHP composer vendored deps — gigantes e nunca alvo de
    # mencao humana no chat.
    "vendor",
    # Rust build output.
    "target",
    # IDE/editor metadata.
    ".idea", ".vscode",
}


@router.get("/search")
async def search_files(
    q: str = "",
    bases: str = "company,repos,agents",
    limit: int = 30,
    _: Principal = Depends(get_principal),
):
    """Busca fuzzy-substring por caminho relativo dentro das bases
    permitidas. Usado pelo @-mention picker do composer. Case-insensitive;
    matches basename primeiro (rank 0) e caminho completo depois (rank 1).
    """
    q_norm = q.strip().lower()
    limit = max(1, min(limit, 100))
    base_keys = [b.strip() for b in bases.split(",") if b.strip() in _BASES]
    if not base_keys:
        base_keys = ["company", "repos", "agents"]

    results: list[tuple[int, str, bool]] = []  # (rank, rel_path, is_dir)
    for key in base_keys:
        base = _BASES[key]
        if not base.exists():
            continue
        base_real = base.resolve()
        for root, dirs, files in walk(str(base_real)):
            dirs[:] = [d for d in dirs if d not in _SEARCH_SKIP_DIRS and not d.startswith(".")]
            root_p = Path(root)
            try:
                rel_root = root_p.relative_to(base_real)
            except ValueError:
                continue
            rel_prefix = f"{key}/{rel_root}".rstrip("/.") if str(rel_root) != "." else key
            # Inclui o proprio diretorio (exceto a raiz base)
            if str(rel_root) != ".":
                name_lower = root_p.name.lower()
                if not q_norm or q_norm in name_lower:
                    results.append((0, rel_prefix, True))
                elif q_norm in rel_prefix.lower():
                    results.append((1, rel_prefix, True))
            for fname in files:
                if fname.startswith("."):
                    continue
                full_rel = f"{rel_prefix}/{fname}"
                name_lower = fname.lower()
                if not q_norm:
                    results.append((0, full_rel, False))
                elif q_norm in name_lower:
                    results.append((0, full_rel, False))
                elif q_norm in full_rel.lower():
                    results.append((1, full_rel, False))
                if len(results) > limit * 4:
                    break
            if len(results) > limit * 4:
                break
        if len(results) > limit * 4:
            break

    # Ordena por (rank, path) e trunca
    results.sort(key=lambda r: (r[0], r[1].lower()))
    out = [{"path": p, "is_dir": d} for (_r, p, d) in results[:limit]]
    return {"q": q, "bases": base_keys, "count": len(out), "entries": out}


@router.get("/read")
async def read_file(path: str, raw: int = 0, _: Principal = Depends(get_principal)):
    """Return file content. With raw=1, always returns raw bytes (for download);
    without it, text files are wrapped in JSON so the viewer can render them.
    """
    full = _resolve(path)
    if not full.exists() or not full.is_file():
        raise HTTPException(status_code=404, detail="file does not exist")
    if full.stat().st_size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file > 50MB")
    mime, _enc = mimetypes.guess_type(str(full))
    mime = mime or "application/octet-stream"
    if raw:
        return FileResponse(full, media_type=mime, filename=full.name)
    # Text-like: try utf-8, fallback binary
    text_prefixes = ("text/", "application/json", "application/xml", "application/yaml")
    if mime.startswith(text_prefixes) or full.suffix.lower() in {
        ".md", ".txt", ".log", ".json", ".yaml", ".yml", ".py", ".js", ".ts",
        ".css", ".html", ".sh", ".sql", ".csv", ".toml", ".ini", ".env",
    }:
        try:
            text = full.read_text(encoding="utf-8")
            return JSONResponse({
                "path": path, "mime": mime, "size": full.stat().st_size,
                "kind": "text", "text": text,
            })
        except UnicodeDecodeError:
            pass
    return FileResponse(full, media_type=mime, filename=full.name)


@router.post("/write")
async def write_file(payload: dict, _: Principal = Depends(get_principal)):
    """Sobreescreve arquivo de texto. Aceita apenas company/ e agents/
    (RW); rejeita repos/ e sessions/ (RO). Cria diretorios pais se faltam.
    """
    rel = (payload.get("path") or "").strip()
    content = payload.get("content")
    if not rel or content is None:
        raise HTTPException(status_code=400, detail="path and content are required")
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="content must be a string")
    if len(content.encode("utf-8")) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="content > 50MB")
    head = rel.lstrip("/").split("/", 1)[0]
    if head in ("repos", "sessions"):
        raise HTTPException(status_code=403, detail=f"{head}/ is read-only")
    full = _resolve(rel)
    if full.exists() and full.is_dir():
        raise HTTPException(status_code=400, detail="path is a directory")
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return {
        "ok": True, "path": rel, "size": full.stat().st_size,
    }


@router.post("/upload")
async def upload_file(file: UploadFile = File(...), principal: Principal = Depends(get_principal)):
    """Upload a file to company/uploads/<yyyymmdd>/<filename>.
    Returns the relative path to be referenced in messages.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="missing file name")

    # Sanitize filename
    safe_name = SAFE_NAME_RE.sub("_", file.filename)
    if not safe_name or safe_name in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid file name")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file > 50MB")

    today = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    dest_dir = COMPANY_DIR / UPLOADS_SUBDIR / today
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Evita colisao: se ja existe, adiciona sufixo -1, -2...
    dest = dest_dir / safe_name
    if dest.exists():
        stem, suffix = dest.stem, dest.suffix
        for i in range(1, 1000):
            candidate = dest_dir / f"{stem}-{i}{suffix}"
            if not candidate.exists():
                dest = candidate
                break

    dest.write_bytes(data)
    rel = f"company/{UPLOADS_SUBDIR}/{today}/{dest.name}"
    mime, _ = mimetypes.guess_type(str(dest))
    return {
        "path": rel, "name": dest.name, "size": len(data),
        "mime": mime or "application/octet-stream",
    }
