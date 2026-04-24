"""`git_commit` — stage and commit everything the agent has written.

Every accepted Coder Agent turn ends with a `git_commit` so checkpoint
restore = `git reset --hard <sha>`. The agent is expected to call this
once per BuildPlan task (roadmap §4 — "feat: scaffold", then one commit
per task).

We reuse `app.sandboxes.git_ops.commit_all` so the commit authorship
matches the rest of the sandbox's agent-driven history (Alloy Agent
<agent@alloy.dev>).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai import RunContext

from app.agents.coder.context import CoderDeps
from app.agents.coder.errors import ToolInputError
from app.agents.coder.results import GitCommitResult
from app.sandboxes.git_ops import _git, commit_all

if TYPE_CHECKING:
    from pydantic_ai import Agent


async def _count_changed(workspace_root: Path) -> int:
    """Count lines in `git diff --cached --name-only` as a cheap proxy for files changed.

    Accurate enough for the LLM's "did my commit actually stage
    anything?" check, and cheap (one git process).
    """
    res = await _git(["diff", "--cached", "--name-only"], workspace_root)
    if not res.ok:
        return 0
    return sum(1 for line in res.stdout.splitlines() if line.strip())


def register(agent: Agent[CoderDeps, str]) -> None:
    @agent.tool
    async def git_commit(
        ctx: RunContext[CoderDeps],
        message: str,
        allow_empty: bool = False,
    ) -> GitCommitResult:
        """Stage every change in the workspace and commit with `message`.

        Returns the commit SHA, or `nothing_to_commit=True` if the
        working tree was clean. Set `allow_empty=True` to leave
        checkpoint markers in the log even when the tree is clean.

        The commit author is always `Alloy Agent <agent@alloy.dev>` —
        the user's own identity never leaks into agent-driven history.
        """
        if not message or not message.strip():
            raise ToolInputError("commit message must be non-empty")

        root = ctx.deps.workspace_root
        ctx.deps.bind(tool="git_commit", message=message, allow_empty=allow_empty).info(
            "coder.tool"
        )

        # Stage first so we can count what's actually about to land.
        await _git(["add", "-A"], root)
        files_changed = await _count_changed(root)

        try:
            sha = await commit_all(root, message, allow_empty=allow_empty)
        except Exception as exc:  # noqa: BLE001 — wrap the sandbox error for the LLM
            raise ToolInputError(f"git commit failed: {exc}") from exc

        if not sha:
            return GitCommitResult(
                sha=None,
                message=message,
                files_changed=0,
                nothing_to_commit=True,
            )
        return GitCommitResult(sha=sha, message=message, files_changed=files_changed)
