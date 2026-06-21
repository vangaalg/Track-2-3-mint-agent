"""deploy.gitsync — round-trip against a LOCAL bare repo (no network)."""

from __future__ import annotations

import subprocess

from deploy import gitsync


def _bare(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)],
                   check=True, capture_output=True)
    return bare


def test_clone_commit_push_roundtrip(tmp_path):
    bare = _bare(tmp_path)
    work = tmp_path / "data"
    gitsync.clone_or_pull(str(bare), into=work, author="t", email="t@t")
    assert (work / ".git").exists()
    # clean tree → nothing to push
    assert gitsync.commit_push(work, msg="init") is False
    # write data → push succeeds, second call is a no-op
    (work / "oi_summary").mkdir(parents=True, exist_ok=True)
    (work / "oi_summary" / "NIFTY.parquet").write_bytes(b"x")
    assert gitsync.commit_push(work, msg="add") is True
    assert gitsync.commit_push(work, msg="again") is False
    # a fresh clone of the same remote sees the pushed file
    other = tmp_path / "other"
    gitsync.clone_or_pull(str(bare), into=other)
    assert (other / "oi_summary" / "NIFTY.parquet").exists()


def test_commit_push_without_git_dir_is_noop(tmp_path):
    assert gitsync.commit_push(tmp_path / "nope") is False
