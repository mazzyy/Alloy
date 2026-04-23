"""Async git helpers for sandbox workspaces.

Every sandbox's workspace *is* a git repo. The scaffolder's `_git_init`
runs once at scaffold time; this module handles the ongoing lifecycle:

* commit after every accepted Coder Agent turn
* `git worktree add` when the Coder Agent edits in multi-file mode
  (rollback = `git worktree remove` + abandon; accept = fast-forward
  merge back into `agent/main`)
* `git reset --hard <sha>` for checkpoint restore

Kept small and subprocess-based. We *don't* pull in pygit2 yet — it's
nice when we're cloning large repos (Phase 3 GitHub work), but for
local per-project ops the cost/benefit is weak.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GitResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


async def _git(args: list[str], cwd: Path, *, env: dict[str, str] | None = None) -> GitResult:
    """Run `git <args>` inside `cwd`.

    `GIT_AUTHOR_*` + `GIT_COMMITTER_*` are set so commits work even in
    sandboxes where the user hasn't configured a global git identity
    (the Coder Agent's commits shouldn't leak the host user's name
    anyway).
    """
    merged_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Alloy Agent",
        "GIT_AUTHOR_EMAIL": "agent@alloy.dev",
        "GIT_COMMITTER_NAME": "Alloy Agent",
        "GIT_COMMITTER_EMAIL": "agent@alloy.dev",
        **(env or {}),
    }
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        env=merged_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return GitResult(
        returncode=proc.returncode or 0,
        stdout=out.decode("utf-8", errors="replace"),
        stderr=err.decode("utf-8", errors="replace"),
    )


async def ensure_repo(cwd: Path) -> None:
    """Initialise a git repo at `cwd` if there isn't one already.

    Idempotent — safe to call on every `manager.boot()`.
    """
    if (cwd / ".git").exists():
        return
    res = await _git(["init", "-b", "main"], cwd)
    if not res.ok:
        from app.sandboxes.types import SandboxError

        raise SandboxError(f"git init failed: {res.stderr.strip()}")


async def commit_all(
    cwd: Path,
    message: str,
    *,
    allow_empty: bool = False,
) -> str:
    """Stage everything and commit. Returns the new commit SHA (or "")
    when there was nothing to commit and `allow_empty=False`.

    `allow_empty=True` lets us put checkpoint markers ("before agent
    turn") into the log even when the tree is clean — useful for
    rollback UX that maps chat turns → commits 1:1.
    """
    from app.sandboxes.types import SandboxError

    add = await _git(["add", "-A"], cwd)
    if not add.ok:
        raise SandboxError(f"git add failed: {add.stderr.strip()}")

    args = ["commit", "-m", message]
    if allow_empty:
        args.append("--allow-empty")
    commit = await _git(args, cwd)
    if not commit.ok:
        # "nothing to commit" is not an error when !allow_empty
        if "nothing to commit" in (commit.stdout + commit.stderr):
            return ""
        raise SandboxError(f"git commit failed: {commit.stderr.strip()}")

    head = await _git(["rev-parse", "HEAD"], cwd)
    if not head.ok:
        raise SandboxError("git rev-parse HEAD failed")
    return head.stdout.strip()


async def head_sha(cwd: Path) -> str:
    res = await _git(["rev-parse", "HEAD"], cwd)
    if not res.ok:
        from app.sandboxes.types import SandboxError

        raise SandboxError(f"git rev-parse HEAD failed: {res.stderr.strip()}")
    return res.stdout.strip()


async def reset_hard(cwd: Path, sha: str) -> None:
    """Move HEAD to `sha`, discarding working-tree + index changes.

    Caller is responsible for running validators after — reset alone
    does not restart containers or reload hot-reloading processes.
    """
    from app.sandboxes.types import SandboxError

    res = await _git(["reset", "--hard", sha], cwd)
    if not res.ok:
        raise SandboxError(f"git reset --hard {sha} failed: {res.stderr.strip()}")


async def add_worktree(cwd: Path, worktree_path: Path, *, base: str = "HEAD") -> None:
    """Create a worktree for isolated multi-file Coder Agent edits.

    Use pattern: `worktree_path = cwd.parent / f"{cwd.name}.wt.<turn_id>"`.
    The agent edits inside the worktree; validators run there; on green
    we fast-forward merge; on red we abandon via `remove_worktree`.
    """
    from app.sandboxes.types import SandboxError

    res = await _git(["worktree", "add", str(worktree_path), base], cwd)
    if not res.ok:
        raise SandboxError(f"git worktree add failed: {res.stderr.strip()}")


async def remove_worktree(cwd: Path, worktree_path: Path) -> None:
    """Abandon a worktree. `--force` because aborted agent edits may
    leave untracked files we want gone."""
    from app.sandboxes.types import SandboxError

    res = await _git(["worktree", "remove", "--force", str(worktree_path)], cwd)
    if not res.ok:
        raise SandboxError(f"git worktree remove failed: {res.stderr.strip()}")
