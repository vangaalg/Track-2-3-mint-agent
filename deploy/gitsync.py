"""Persist the recorder's ``data/`` to a private git repo (Railway has no durable disk).

The recorder writes parquet under ``data/``; an ephemeral container would lose it on
restart. We make a dedicated **private data repo** the durable store: clone it into
``data/`` on startup (restores prior parquet), and commit + push periodically + at
session close. Secrets never enter the repo — only parquet. Plain subprocess ``git``
(present in the Railway/nixpacks image); auth via a PAT embedded in ``DATA_REPO_URL``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _git(args, cwd=None, check=False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, check=check,
                          capture_output=True, text=True)


def _set_identity(into: Path, author=None, email=None) -> None:
    a = author or os.environ.get("GIT_AUTHOR_NAME", "recorder")
    e = email or os.environ.get("GIT_AUTHOR_EMAIL", "recorder@local")
    _git(["config", "user.name", a], cwd=into)
    _git(["config", "user.email", e], cwd=into)


def clone_or_pull(repo_url: str, into: str | Path = "data",
                  author=None, email=None) -> None:
    """Restore the data repo into ``into``: ``git pull`` if already a clone, else clone.

    On a fresh container ``into`` doesn't exist yet → clone (an empty repo clones fine).
    Sets the commit identity afterwards so the first ``commit_push`` works.
    """
    into = Path(into)
    if (into / ".git").exists():
        _git(["pull", "--ff-only"], cwd=into)
    else:
        into.parent.mkdir(parents=True, exist_ok=True)
        _git(["clone", repo_url, str(into)])
    if (into / ".git").exists():
        _set_identity(into, author, email)


def commit_push(path: str | Path = "data", msg: str = "recorder snapshot",
                branch: str = "main") -> bool:
    """Stage + commit + push everything under ``path``. No-op (returns False) when the
    working tree is clean or push fails; returns True on a pushed commit."""
    p = Path(path)
    if not (p / ".git").exists():
        return False
    _git(["add", "-A"], cwd=p)
    if not _git(["status", "--porcelain"], cwd=p).stdout.strip():
        return False                                  # nothing changed
    _git(["commit", "-m", msg], cwd=p)
    return _git(["push", "origin", f"HEAD:{branch}"], cwd=p).returncode == 0
