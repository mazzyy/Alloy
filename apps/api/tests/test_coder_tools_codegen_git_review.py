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


# The generic-question rejection logic is the Phase-1 fix for an agent
# regression where the Coder Agent escalated `request_human_review`
# after a single apply_patch context-mismatch with a content-free
# question ("what file should I (re)attempt now?"). The tool now feeds
# that back as ModelRetry so the agent gets one more turn to either
# write a real question or — better — go retry the underlying tool.


def test_looks_generic_rejects_too_short_question() -> None:
    from app.agents.coder.tools.review import _looks_generic

    reason = _looks_generic("help?")
    assert reason is not None
    assert "too short" in reason


def test_looks_generic_rejects_what_should_i_do() -> None:
    from app.agents.coder.tools.review import _looks_generic

    # Long enough to clear the length floor, but matches a generic
    # template fragment.
    reason = _looks_generic(
        "What should I do next? The previous attempt failed and I am "
        "uncertain how to proceed."
    )
    assert reason is not None
    assert "generic placeholder" in reason
    assert "what should i do" in reason


def test_looks_generic_rejects_what_specific_file_or_change() -> None:
    from app.agents.coder.tools.review import _looks_generic

    # The exact wording from the regression that motivated this guard.
    reason = _looks_generic(
        "The previous attempt to apply a patch failed due to context "
        "mismatch. I need clarification: what specific file or change "
        "should I (re)attempt now?"
    )
    assert reason is not None
    assert "generic placeholder" in reason


def test_looks_generic_accepts_specific_question() -> None:
    from app.agents.coder.tools.review import _looks_generic

    # Has length, includes what was tried + the concrete ambiguity.
    reason = _looks_generic(
        "alembic_autogenerate produced op.drop_table('legacy_users') "
        "alongside the new users.created_at column. The drop is "
        "destructive — should I keep the legacy_users table and only "
        "add the column, or proceed with the drop?"
    )
    assert reason is None


# 7th-regression phrasings — the user reported a build pause with this
# question shape: "The previous apply_patch retries exceeded
# (UnexpectedModelBehavior). I need clarification which file and change
# you want me to make next. What should I edit?". The original filter
# only caught "what should I do" / "what specific file or change". We
# expanded the fragment list to catch "what should I edit", "which file
# and change", "I need clarification", and the `UnexpectedModelBehavior`
# leak — these tests pin those phrasings against future regressions.


def test_looks_generic_rejects_what_should_i_edit() -> None:
    from app.agents.coder.tools.review import _looks_generic

    reason = _looks_generic(
        "The previous attempt failed and I am unsure how to proceed. "
        "What should I edit in the next turn?"
    )
    assert reason is not None
    assert "generic placeholder" in reason
    assert "what should i edit" in reason


def test_looks_generic_rejects_which_file_and_change() -> None:
    from app.agents.coder.tools.review import _looks_generic

    # The exact phrase from the 7th regression's pause message.
    reason = _looks_generic(
        "The previous apply_patch retries exceeded the budget. I need "
        "clarification which file and change you want me to make next."
    )
    assert reason is not None
    assert "generic placeholder" in reason


def test_looks_generic_rejects_unexpected_model_behavior_leak() -> None:
    from app.agents.coder.tools.review import _looks_generic

    # If the agent leaks pydantic-ai's exception class name into its
    # question we want to reject it — the human can't act on
    # `UnexpectedModelBehavior('Exceeded maximum retries (1)')`.
    reason = _looks_generic(
        "The previous apply_patch retries exceeded "
        "(UnexpectedModelBehavior). Please advise on a path forward."
    )
    assert reason is not None
    assert "generic placeholder" in reason
    # Either fragment may match first; the important thing is that the
    # leaky question is rejected.
    assert (
        "unexpectedmodelbehavior" in reason
        or "exceeded maximum retries" in reason
    )


def test_looks_generic_rejects_i_need_clarification() -> None:
    from app.agents.coder.tools.review import _looks_generic

    reason = _looks_generic(
        "The patch could not be applied successfully on the last turn. "
        "I need clarification before I retry the operation."
    )
    assert reason is not None
    assert "generic placeholder" in reason
    assert "i need clarification" in reason


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


# 8th-regression — the agent gamed silent-giveup detection by issuing
# `git_commit("checkpoint", allow_empty=True)` after a failed apply_patch
# and writing a >80-char rationalisation summary, which the build's
# commit_sha-is-not-None heuristic accepted as a real edit. The fix
# blocks empty commits at the tool boundary by raising `ModelRetry`
# whenever `allow_empty=True` is passed with zero staged changes. This
# test asserts the rejection fires and surfaces the corrective nudge.


@pytest.mark.skipif(not _have_git(), reason="git binary not on PATH")
async def test_git_commit_rejects_empty_allow_empty_commit(workspace: Path) -> None:
    from pydantic_ai import ModelRetry, RunContext

    # Real repo so `git diff --cached` works; no staged changes either,
    # so files_changed will be 0.
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

    deps = _deps(workspace)

    # We need to invoke the tool's body directly. Use `register` to wire
    # it onto a stub agent and then poke the registered callable. The
    # tool body returns a coroutine — capture it via the `@agent.tool`
    # decorator's call.
    captured: dict[str, Any] = {}

    class _StubAgent:
        def tool(self, fn: Any) -> Any:
            captured["fn"] = fn
            return fn

    git_mod.register(_StubAgent())  # type: ignore[arg-type]
    git_commit_fn = captured["fn"]

    # Build a minimal RunContext-like object — only `.deps` is read.
    class _Ctx:
        def __init__(self, deps: CoderDeps) -> None:
            self.deps = deps

    ctx: RunContext[CoderDeps] = _Ctx(deps)  # type: ignore[assignment]

    with pytest.raises(ModelRetry) as excinfo:
        await git_commit_fn(ctx, "checkpoint", allow_empty=True)

    msg = str(excinfo.value)
    assert "git_commit refused" in msg
    assert "allow_empty=True" in msg
    # The retry prompt must direct the agent to fix the underlying edit
    # instead of routing around it.
    assert "apply_patch" in msg or "write_file" in msg


# Mako trailing-whitespace fix — the alembic-generated migration ships
# with W291 trailing whitespace on blank lines (a generic.py.mako
# artefact). The agent can't fix files alembic owns, so the codegen tool
# auto-runs `ruff format` + `ruff check --fix --select W291,...` against
# the new migration immediately. This test asserts those follow-up calls
# happen and the migration path is added to deps.touched_paths.


async def test_alembic_autogenerate_runs_ruff_format_after_generation(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration = workspace / "apps/api/alembic/versions/aaaa11112222_add_task.py"
    migration.parent.mkdir(parents=True, exist_ok=True)
    migration.write_text(
        "def upgrade():\n"
        "    op.create_table('task', ...)\n"  # noqa: E501 — irrelevant
        "def downgrade():\n"
        "    pass\n",
        encoding="utf-8",
    )

    calls: list[tuple[str, list[str]]] = []

    async def runner(
        _deps: CoderDeps,
        binary: str,
        args: list[str],
        **_kwargs: Any,
    ) -> CommandResult:
        calls.append((binary, args))
        # Only the alembic call needs the "Generating ..." preamble; the
        # ruff calls just need to return ok=0.
        if binary == "alembic":
            return CommandResult(
                command="alembic revision ...",
                returncode=0,
                stdout=f"Generating {migration} ... done\n",
                stderr="",
                duration_s=1.0,
            )
        return CommandResult(
            command=f"{binary} ...",
            returncode=0,
            stdout="",
            stderr="",
            duration_s=0.1,
        )

    monkeypatch.setattr(codegen_mod, "run_command", runner)

    deps = _deps(workspace)
    result = await _alembic_autogenerate(deps, "add task table")

    assert result.ok is True
    assert result.migration_path is not None

    # Three calls: alembic, ruff format, ruff check --fix --select ...
    binaries = [b for b, _ in calls]
    assert binaries[0] == "alembic"
    assert "ruff" in binaries[1:], (
        f"expected ruff follow-up calls after alembic, got {binaries!r}"
    )

    # Find the ruff format call — must target the migration path.
    ruff_format_calls = [args for b, args in calls if b == "ruff" and args[:1] == ["format"]]
    assert ruff_format_calls, f"missing `ruff format` call in {calls!r}"
    assert any(result.migration_path in args for args in ruff_format_calls)

    # Find the ruff check --fix call — must include the W291 selector.
    ruff_check_calls = [args for b, args in calls if b == "ruff" and "check" in args]
    assert ruff_check_calls, f"missing `ruff check --fix` call in {calls!r}"
    fix_args = ruff_check_calls[0]
    assert "--fix" in fix_args
    assert "--select" in fix_args
    selector_idx = fix_args.index("--select")
    selector = fix_args[selector_idx + 1]
    assert "W291" in selector

    # The migration path must be added to deps.touched_paths so the
    # validator scope picks up any genuine alembic problems on the same
    # turn rather than leaving them to next attempt.
    assert result.migration_path in deps.touched_paths
