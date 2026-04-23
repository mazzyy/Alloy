"""Git helper tests — run against a real `git` on a tmp dir.

Skipped if `git` isn't on PATH. CI always has it; devs typically do too.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.sandboxes.git_ops import (
    add_worktree,
    commit_all,
    ensure_repo,
    head_sha,
    remove_worktree,
    reset_hard,
)
from app.sandboxes.types import SandboxError

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


async def test_ensure_repo_initialises_once(tmp_path: Path):
    await ensure_repo(tmp_path)
    assert (tmp_path / ".git").is_dir()
    # Idempotent.
    await ensure_repo(tmp_path)


async def test_commit_all_records_sha(tmp_path: Path):
    await ensure_repo(tmp_path)
    (tmp_path / "README.md").write_text("hi\n", encoding="utf-8")
    sha = await commit_all(tmp_path, "feat: initial")
    assert len(sha) == 40
    assert await head_sha(tmp_path) == sha


async def test_commit_all_returns_empty_when_nothing_to_commit(tmp_path: Path):
    await ensure_repo(tmp_path)
    (tmp_path / "README.md").write_text("hi\n", encoding="utf-8")
    await commit_all(tmp_path, "feat: initial")
    # No new changes — commit should be a no-op without --allow-empty.
    result = await commit_all(tmp_path, "feat: nothing")
    assert result == ""


async def test_commit_all_allow_empty(tmp_path: Path):
    await ensure_repo(tmp_path)
    (tmp_path / "README.md").write_text("hi\n", encoding="utf-8")
    first = await commit_all(tmp_path, "feat: initial")
    empty = await commit_all(tmp_path, "checkpoint", allow_empty=True)
    assert empty and empty != first


async def test_reset_hard_moves_head_back(tmp_path: Path):
    await ensure_repo(tmp_path)
    (tmp_path / "a.txt").write_text("first\n", encoding="utf-8")
    s1 = await commit_all(tmp_path, "feat: a")
    (tmp_path / "a.txt").write_text("second\n", encoding="utf-8")
    s2 = await commit_all(tmp_path, "feat: a v2")
    assert s1 != s2
    await reset_hard(tmp_path, s1)
    assert await head_sha(tmp_path) == s1
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "first\n"


async def test_reset_hard_fails_on_bogus_sha(tmp_path: Path):
    await ensure_repo(tmp_path)
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    await commit_all(tmp_path, "feat")
    with pytest.raises(SandboxError):
        await reset_hard(tmp_path, "0" * 40)


async def test_worktree_add_and_remove(tmp_path: Path):
    await ensure_repo(tmp_path)
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    await commit_all(tmp_path, "feat")
    worktree = tmp_path.parent / (tmp_path.name + ".wt")
    try:
        await add_worktree(tmp_path, worktree)
        assert worktree.is_dir()
        assert (worktree / "a.txt").read_text(encoding="utf-8") == "x"
    finally:
        if worktree.exists():
            await remove_worktree(tmp_path, worktree)
    assert not worktree.exists()
