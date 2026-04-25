"""Unit tests for codegen / git / review tools.

Codegen tools shell out — we mock `run_command` to return canned
outcomes. The git tool needs a real `git` binary + a temp repo; if
that's missing (rare), the test is skipped with a clear message. The
review tool is pure in-memory.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

import app.agents.coder.tools.codegen as codegen_mod
import app.agents.coder.tools.git as git_mod
from app.agents.coder.context import CoderDeps
from app.agents.coder.errors import HumanReviewRequired, ToolInputError
from app.agents.coder.results import CommandResult
from app.agents.coder.tools.codegen import _alembic_autogenerate


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "apps/api/alembic/versions").mkdir(parents=True)
    return tmp_path


def _deps(workspace: Path) -> CoderDeps:
    return CoderDeps(workspace_root=workspace, turn_id="t", project_id="p")


# ── alembic_autogenerate ───────────────────────────────────────────────


async def _mock_run_command(outcome: CommandResult) -> Any:
    # Accept arbitrary keyword args so callers can add `cwd_subdir` etc.
    # without breaking the mock contract every time.
    async def runner(
        _deps: CoderDeps,
        binary: str,
        args: list[str],
        **_kwargs: Any,
    ):
        return outcome

    return runner


async def test_alembic_autogenerate_flags_destructive_ops(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pretend alembic wrote a migration that drops a table.
    migration = workspace / "apps/api/alembic/versions/abcdef123456_drop_users.py"
    migration.parent.mkdir(parents=True, exist_ok=True)
    migration.write_text(
        "def upgrade():\n"
        "    op.drop_table('users')\n"
        "def downgrade():\n"
        "    pass\n",
        encoding="utf-8",
    )
    out = f"Generating {migration} ... done\n"
    monkeypatch.setattr(
        codegen_mod,
        "run_command",
        await _mock_run_command(
            CommandResult(
                command="alembic revision ...",
                returncode=0,
                stdout=out,
                stderr="",
                duration_s=1.0,
            )
        ),
    )

    result = await _alembic_autogenerate(_deps(workspace), "drop users")
    assert "op.drop_table" in result.destructive_ops
    assert result.ok is True
    assert result.migration_path is not None
    assert result.migration_path.endswith("abcdef123456_drop_users.py")


async def test_alembic_autogenerate_rejects_empty_message(workspace: Path) -> None:
    with pytest.raises(ToolInputError, match="non-empty"):
        await _alembic_autogenerate(_deps(workspace), "   ")


async def test_alembic_autogenerate_with_no_destructive_ops(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration = workspace / "apps/api/alembic/versions/0001abcd_add_users.py"
    migration.parent.mkdir(parents=True, exist_ok=True)
    migration.write_text(
        "def upgrade():\n"
        "    op.create_table('users', ...)\n"
        "def downgrade():\n"
        "    pass\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        codegen_mod,
        "run_command",
        await _mock_run_command(
            CommandResult(
                command="alembic revision ...",
                returncode=0,
                stdout=f"Generating {migration} ... done\n",
                stderr="",
                duration_s=1.0,
            )
        ),
    )
    result = await _alembic_autogenerate(_deps(workspace), "add users")
    assert result.destructive_ops == []
    assert result.ok is True


# ── request_human_review ───────────────────────────────────────────────


def test_human_review_required_carries_question_and_options() -> None:
    exc = HumanReviewRequired("Proceed with drop_table?", options=["yes", "no"])
    assert exc.question == "Proceed with drop_table?"
    assert exc.options == ["yes", "no"]


# ── git_commit ─────────────────────────────────────────────────────────


def _have_git() -> bool:
    return shutil.which("git") is not None


@pytest.mark.skipif(not _have_git(), reason="git binary not on PATH")
async def test_git_commit_records_sha_for_new_file(workspace: Path) -> None:
    # Initialise a repo. Older git builds (what ships in miniforge and
    # the Xcode CLI on macOS) don't understand `git init -b main`, so we
    # set the default branch name via symbolic-ref after plain init.
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )

    (workspace / "README.md").write_text("hello\n", encoding="utf-8")

    # Simulate the tool's public interface by calling the inner helpers
    # — the agent-registered tool calls the same code path.
    from app.sandboxes.git_ops import commit_all

    deps = _deps(workspace)
    # Stage + commit via the helper used inside the tool.
    sha = await commit_all(deps.workspace_root, "feat: initial README")
    assert sha
    # Sanity: the repo now has a HEAD at that SHA.
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=workspace, text=True
    ).strip()
    assert head == sha


# Probe the helper directly so we still cover the tool when git is
# missing — `_count_changed` returns 0 when `git` isn't wired up, which
# is the right defensive behavior.


async def test_count_changed_on_missing_repo_returns_zero(tmp_path: Path) -> None:
    # No `.git` dir, so `git diff --cached` errors out cleanly.
    count = await git_mod._count_changed(tmp_path)
    assert count == 0
