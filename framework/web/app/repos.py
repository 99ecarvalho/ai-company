"""Create new git repos in the shared repos workspace on behalf of agents.

Agent containers mount the repos workspace read-only (D-115): they can't
edit code outside their worktrees, and only each existing repo's `.git/` is
writable. That also means an agent can't create a repo, and a repo created
after its container started would never get a writable `.git/` mount.

So the web container (which mounts the workspace read-write) creates repos
for them, with the git dir kept apart from the working tree:

    <repos>/<name>/.git            file: "gitdir: ../.gitdirs/<name>.git"
    <repos>/.gitdirs/<name>.git/   the actual git dir

reconcile mounts `<repos>/.gitdirs` read-write into agents that write to
repos, so `git worktree add`/`fetch` work for every repo created this way,
including ones created while the agent is running. The gitdir pointer is
relative, so the repo also works from the host.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPOS_ROOT = Path(os.environ.get("WORKSPACE_REPOS", "/workspace/repos"))
GITDIRS_NAME = ".gitdirs"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

# Agent containers run as `node` (uid/gid 1000). Repos must belong to that
# user or git refuses to touch them ("dubious ownership").
AGENT_UID = int(os.environ.get("AGENT_UID", "1000"))
AGENT_GID = int(os.environ.get("AGENT_GID", "1000"))

COMMIT_NAME = os.environ.get("GIT_AUTHOR_NAME") or "ai-company"
COMMIT_EMAIL = os.environ.get("GIT_AUTHOR_EMAIL") or "agents@local"


class RepoError(ValueError):
    """Invalid request (bad name, path taken by something that isn't a repo)."""


def _git(*args: str, cwd: Path | None = None) -> str:
    # Repos belong to the agents' uid, not to us (we may run as root):
    # without safe.directory git refuses them as "dubious ownership".
    proc = subprocess.run(
        ["git", "-c", "safe.directory=*", *args], cwd=cwd, capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _chown_tree(path: Path) -> None:
    if os.geteuid() != 0:
        return  # not root: files already belong to whoever runs us
    for root, dirs, files in os.walk(path):
        os.chown(root, AGENT_UID, AGENT_GID)
        for f in files:
            os.chown(os.path.join(root, f), AGENT_UID, AGENT_GID, follow_symlinks=False)


def init_repo(name: str, root: Path = REPOS_ROOT) -> dict:
    """Create `<root>/<name>` with one empty commit on `main`. Idempotent.

    Returns {repo, path, head_sha, created}."""
    if not NAME_RE.match(name or ""):
        raise RepoError(f"invalid repo name {name!r}: use kebab-case (a-z, 0-9, '-')")
    repo_dir = root / name
    gitdirs = root / GITDIRS_NAME
    git_dir = gitdirs / f"{name}.git"

    if (repo_dir / ".git").exists():
        head = _git("-C", str(repo_dir), "rev-parse", "HEAD")
        return {"repo": name, "path": str(repo_dir), "head_sha": head, "created": False}
    if repo_dir.exists() and any(repo_dir.iterdir()):
        raise RepoError(f"{repo_dir} already exists and is not a git repo")
    if git_dir.exists():
        raise RepoError(f"{git_dir} already exists without a matching working tree")

    gitdirs.mkdir(exist_ok=True)
    _chown_tree(gitdirs)
    try:
        _git("init", "--quiet", "--initial-branch=main", f"--separate-git-dir={git_dir}", str(repo_dir))
        # git writes an absolute gitdir path; make it relative so the same
        # repo works on the host and in every container, whatever the mount point.
        (repo_dir / ".git").write_text(f"gitdir: ../{GITDIRS_NAME}/{name}.git\n")
        _git("-C", str(repo_dir), "-c", f"user.name={COMMIT_NAME}", "-c", f"user.email={COMMIT_EMAIL}",
             "commit", "--quiet", "--allow-empty", "-m", "Initial commit")
        head = _git("-C", str(repo_dir), "rev-parse", "HEAD")
    except Exception:
        # Leave nothing half-created behind; a retry starts clean.
        import shutil
        shutil.rmtree(repo_dir, ignore_errors=True)
        shutil.rmtree(git_dir, ignore_errors=True)
        raise
    _chown_tree(repo_dir)
    _chown_tree(git_dir)
    return {"repo": name, "path": str(repo_dir), "head_sha": head, "created": True}
