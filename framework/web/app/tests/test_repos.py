"""Tests for app/repos.py (repo creation on behalf of agents).

Run inside the web container:
  docker compose exec web python -m unittest app.tests.test_repos -v
"""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.repos import RepoError, init_repo


def _git(*args: str) -> str:
    # Like the agents, which own the repos; when tests run as root (web
    # image), init_repo hands them to uid 1000.
    return subprocess.run(["git", "-c", "safe.directory=*", *args], check=True, capture_output=True, text=True).stdout.strip()


class InitRepo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "repos"
        self.root.mkdir()

    def tearDown(self):
        for dirpath, _, _ in os.walk(self._tmp.name):
            os.chmod(dirpath, stat.S_IRWXU)
        self._tmp.cleanup()

    def test_creates_repo_with_relative_gitdir(self):
        out = init_repo("demo", root=self.root)
        self.assertTrue(out["created"])
        self.assertRegex(out["head_sha"], r"^[0-9a-f]{40}$")
        self.assertEqual((self.root / "demo" / ".git").read_text(), "gitdir: ../.gitdirs/demo.git\n")
        self.assertTrue((self.root / ".gitdirs" / "demo.git" / "HEAD").is_file())
        self.assertEqual(_git("-C", str(self.root / "demo"), "branch", "--show-current"), "main")

    def test_idempotent(self):
        first = init_repo("demo", root=self.root)
        again = init_repo("demo", root=self.root)
        self.assertFalse(again["created"])
        self.assertEqual(again["head_sha"], first["head_sha"])

    def test_worktree_add_with_read_only_working_tree(self):
        # Mirrors the agent container: working tree RO, .gitdirs RW (D-115).
        init_repo("demo", root=self.root)
        os.chmod(self.root / "demo", stat.S_IRUSR | stat.S_IXUSR)
        wt = Path(self._tmp.name) / "worktrees" / "demo" / "t1"
        _git("-C", str(self.root / "demo"), "worktree", "add", "-q", "-b", "task/t1", str(wt))
        self.assertEqual(_git("-C", str(wt), "branch", "--show-current"), "task/t1")

    def test_rejects_bad_names(self):
        for name in ("", "Demo", "../x", "a.b", "-x", "a" * 64):
            with self.assertRaises(RepoError, msg=name):
                init_repo(name, root=self.root)

    def test_rejects_non_repo_dir(self):
        (self.root / "taken").mkdir()
        (self.root / "taken" / "file.txt").write_text("x")
        with self.assertRaises(RepoError):
            init_repo("taken", root=self.root)


if __name__ == "__main__":
    unittest.main()
