"""Integration tests for the LangGraph build loop.

We stub `run_task_with_validators` so the tests don't call Azure; the
LangGraph + state + edge logic is what we're exercising. Each test
initializes a real git repo in `tmp_path` (the graph calls
`git_ops.head_sha` and sometimes `git_ops.reset_hard` — stubbing those
would lose the guarantee that failed tasks actually roll back).

Checkpointer in tests: `MemorySaver` via `checkpointer_kind="memory"`
or directly-injected. We exercise the on-disk `AsyncSqliteSaver` in a
single dedicated test so the bulk of the suite stays fast.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver

from alloy_shared.plan import BuildPlan, FileOp, FileOpKind
from app.agents.build import runner as runner_mod
from app.agents.build import graph as graph_mod
from app.agents.build.state import BuildOutcome, HumanReviewPayload, TaskOutcome
from app.agents.coder.context import CoderDeps
from app.agents.coder.errors import HumanReviewRequired
from app.agents.coder.results import (
    CommandResult,
    ValidatorIssue,
    ValidatorLoopAttempt,
    ValidatorLoopResult,
    ValidatorReport,
)


pytestmark = pytest.mark.asyncio


# ── Test helpers ──────────────────────────────────────────────────────


def _op(op_id: str, *, depends_on: list[str] | None = None) -> FileOp:
    return FileOp(
        kind=FileOpKind.create,
        path=f"apps/api/app/models/{op_id}.py",
        intent=f"Add {op_id} model",
        depends_on=depends_on or [],
        id=op_id,
    )


def _plan(*ops: FileOp) -> BuildPlan:
    return BuildPlan(spec_slug="test-build", ops=list(ops))


def _ok_result(*, attempts: int = 1) -> ValidatorLoopResult:
    report = ValidatorReport(ok=True, issue_count=0, issues=[], commands=[])
    return ValidatorLoopResult(
        ok=True,
        attempts_used=attempts,
        max_attempts=3,
        attempts=[
            ValidatorLoopAttempt(
                attempt=i,
                agent_output=f"turn {i}",
                agent_turn_count=1,
                report=report,
                agent_error=None,
            )
            for i in range(1, attempts + 1)
        ],
        final_report=report,
    )


def _fail_result() -> ValidatorLoopResult:
    issues = [
        ValidatorIssue(
            tool="ruff",
            path="apps/api/app/models/order.py",
            line=3,
            code="F401",
            message="`os` imported but unused",
        )
    ]
    report = ValidatorReport(
        ok=False,
        issue_count=1,
        issues=issues,
        commands=[
            CommandResult(
                command="ruff check",
                returncode=1,
                stdout="",
                stderr="",
                duration_s=0.1,
            )
        ],
    )
    return ValidatorLoopResult(
        ok=False,
        attempts_used=3,
        max_attempts=3,
        attempts=[
            ValidatorLoopAttempt(
                attempt=i,
                agent_output=f"try {i}",
                agent_turn_count=1,
                report=report,
                agent_error=None,
            )
            for i in range(1, 4)
        ],
        final_report=report,
    )


async def _git(args: list[str], cwd: Path) -> None:
    """Run git synchronously in a test fixture. We don't go through
    git_ops to avoid tangling the test setup with the production
    helpers — plain subprocess is fine for scaffolding a repo."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        env={
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@alloy.dev",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@alloy.dev",
            "HOME": str(cwd),
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    assert proc.returncode == 0, f"git {args} failed: {err.decode()}"


@pytest.fixture
async def workspace(tmp_path: Path) -> Path:
    """Initialised git workspace with one committed file.

    The graph's `git_ops.head_sha` requires at least one commit on
    HEAD; without this fixture the pre-task SHA lookup would fail
    before we got anywhere useful in the test.
    """
    (tmp_path / "apps" / "api" / "app" / "models").mkdir(parents=True)
    (tmp_path / "README.md").write_text("# test\n", encoding="utf-8")
    # Pre-2.28 git (Xcode CLI tools, miniforge bundle) doesn't accept
    # `init -b <branch>` — match the portable pattern used in
    # `git_ops.ensure_repo`.
    await _git(["init"], tmp_path)
    await _git(["symbolic-ref", "HEAD", "refs/heads/main"], tmp_path)
    await _git(["add", "-A"], tmp_path)
    await _git(["commit", "-m", "initial"], tmp_path)
    return tmp_path


# ── Happy path ────────────────────────────────────────────────────────


async def test_linear_plan_runs_all_tasks_in_order(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three tasks, all green. Outcomes come back in dependency order
    and every task is marked ok."""
    called_prompts: list[str] = []

    async def fake_inner(
        task_prompt: str,
        deps: CoderDeps,
        *,
        validator_targets: list[str] | None = None,
        max_attempts: int = 3,
        agent: Any = None,
    ) -> ValidatorLoopResult:
        called_prompts.append(task_prompt)
        return _ok_result()

    monkeypatch.setattr(graph_mod, "run_task_with_validators", fake_inner)

    plan = _plan(
        _op("a"),
        _op("b", depends_on=["a"]),
        _op("c", depends_on=["b"]),
    )
    deps = CoderDeps(workspace_root=workspace, project_id="test-proj")

    outcome = await runner_mod.run_build_plan(
        plan, deps, checkpointer=MemorySaver()
    )

    assert outcome.ok is True
    assert outcome.tasks_run == 3
    assert outcome.tasks_total == 3
    assert [o.task_id for o in outcome.outcomes] == ["a", "b", "c"]
    assert all(o.ok for o in outcome.outcomes)
    assert outcome.pending_review is None
    # thread_id is deterministic when project_id is set.
    assert outcome.thread_id == "project:test-proj"
    # The prompt surfaces both the task intent and the id.
    assert "a" in called_prompts[0] and "Add a model" in called_prompts[0]


async def test_linear_plan_clears_touched_paths_between_tasks(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: each task must start with an empty
    `touched_paths` set so the validator loop scopes ruff/mypy to
    *this* task's writes only. If we accumulated across tasks the
    later tasks would appear to have touched files they didn't,
    spilling validator scope."""
    snapshots: list[set[str]] = []

    async def fake_inner(
        task_prompt: str,
        deps: CoderDeps,
        *,
        validator_targets: list[str] | None = None,
        max_attempts: int = 3,
        agent: Any = None,
    ) -> ValidatorLoopResult:
        snapshots.append(set(deps.touched_paths))
        # Pretend this task wrote something so the NEXT task's snapshot
        # would be non-empty if we didn't clear.
        deps.touched_paths.add(f"touched-by-{task_prompt.splitlines()[0]}")
        return _ok_result()

    monkeypatch.setattr(graph_mod, "run_task_with_validators", fake_inner)

    plan = _plan(_op("a"), _op("b", depends_on=["a"]))
    deps = CoderDeps(workspace_root=workspace, project_id="scoping")

    await runner_mod.run_build_plan(plan, deps, checkpointer=MemorySaver())

    assert snapshots == [set(), set()], (
        "task_node must clear deps.touched_paths before each task; "
        f"got snapshots={snapshots}"
    )


# ── Failure path ──────────────────────────────────────────────────────


async def test_middle_task_failure_skips_subsequent_tasks_and_rolls_back(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If task 2 of 3 fails, task 3 must NOT run and the repo must be
    at the pre-task-2 SHA. The first task's commit stays (it was
    green)."""
    call_log: list[str] = []

    async def fake_inner(
        task_prompt: str,
        deps: CoderDeps,
        *,
        validator_targets: list[str] | None = None,
        max_attempts: int = 3,
        agent: Any = None,
    ) -> ValidatorLoopResult:
        first_line = task_prompt.splitlines()[0]
        call_log.append(first_line)
        # Agent writes + commits as part of the task — simulate with a
        # real git commit so pre/post SHAs differ. The prompt format is
        # "Create <path>: <intent>", so token[1] carries a trailing colon
        # we need to strip before using it as a filename.
        fname = first_line.split()[1].rstrip(":")
        target = workspace / fname
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {first_line}\n", encoding="utf-8")
        await _git(["add", "-A"], workspace)
        await _git(["commit", "-m", first_line], workspace)
        # Task "b" fails validators; everything else is green.
        if "models/b.py" in task_prompt:
            return _fail_result()
        return _ok_result()

    monkeypatch.setattr(graph_mod, "run_task_with_validators", fake_inner)

    plan = _plan(
        _op("a"),
        _op("b", depends_on=["a"]),
        _op("c", depends_on=["b"]),
    )
    deps = CoderDeps(workspace_root=workspace, project_id="failtest")

    outcome = await runner_mod.run_build_plan(
        plan, deps, checkpointer=MemorySaver()
    )

    assert outcome.ok is False
    assert outcome.tasks_run == 2, "failed task records an outcome; skipped tasks don't"
    assert outcome.tasks_total == 3
    assert [o.task_id for o in outcome.outcomes] == ["a", "b"]
    assert outcome.outcomes[0].ok is True
    assert outcome.outcomes[0].commit_sha is not None
    assert outcome.outcomes[1].ok is False
    # Failed task rolled back → no enduring commit sha on that outcome.
    assert outcome.outcomes[1].commit_sha is None
    assert outcome.outcomes[1].error is not None
    assert "F401" in outcome.outcomes[1].error
    # Task "c" was NEVER called — the edge router ended the graph.
    assert not any("models/c.py" in p for p in call_log)

    # Repo state: task-a's file exists on disk (its commit survived),
    # task-b's file does NOT (reset rolled it back).
    assert (workspace / "apps/api/app/models/a.py").exists()
    assert not (workspace / "apps/api/app/models/b.py").exists()


async def test_cycle_in_plan_surfaces_as_cycle_error(
    workspace: Path,
) -> None:
    """PlanCycleError escapes the graph — the build never reaches any
    task_node so there's nothing to checkpoint."""
    from app.agents.build.topo import PlanCycleError

    plan = _plan(
        _op("a", depends_on=["b"]),
        _op("b", depends_on=["a"]),
    )
    deps = CoderDeps(workspace_root=workspace)
    with pytest.raises(PlanCycleError):
        await runner_mod.run_build_plan(plan, deps, checkpointer=MemorySaver())


# ── Human-review interrupt + resume ───────────────────────────────────


async def test_human_review_pauses_and_resume_completes_build(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 1 raises HumanReviewRequired → graph pauses. Caller supplies
    an answer via `resume_build()` → same task re-runs with the answer
    appended to the prompt → both tasks complete green."""
    attempts: list[dict[str, Any]] = []

    async def fake_inner(
        task_prompt: str,
        deps: CoderDeps,
        *,
        validator_targets: list[str] | None = None,
        max_attempts: int = 3,
        agent: Any = None,
    ) -> ValidatorLoopResult:
        attempts.append({"prompt": task_prompt})
        # Realistic agent behaviour: task "a" keeps asking the same
        # question every time the prompt doesn't contain the resolved
        # answer. That matches how a real Coder Agent would behave,
        # and it also matches LangGraph's re-execution model — the
        # node is replayed from the top on resume, so task_node's
        # inner `run_task_with_validators` call re-raises, the
        # except-block runs, and `interrupt()` returns the answer.
        if "models/a.py" in task_prompt and "Human review response" not in task_prompt:
            raise HumanReviewRequired(
                question="Approve destructive rename?",
                options=["yes", "no"],
            )
        return _ok_result()

    monkeypatch.setattr(graph_mod, "run_task_with_validators", fake_inner)

    plan = _plan(_op("a"), _op("b", depends_on=["a"]))
    deps = CoderDeps(workspace_root=workspace, project_id="resume-test")
    saver = MemorySaver()

    # First invocation — graph pauses on task a's HumanReviewRequired.
    outcome = await runner_mod.run_build_plan(plan, deps, checkpointer=saver)

    assert outcome.ok is False, "paused build is not yet successful"
    assert outcome.pending_review is not None
    assert outcome.pending_review.task_id == "a"
    assert outcome.pending_review.question == "Approve destructive rename?"
    assert outcome.pending_review.options == ["yes", "no"]
    # Nothing in `completed` yet — task a hasn't finished.
    assert outcome.outcomes == []

    # Resume with the answer. Same thread_id so the checkpointer
    # picks up where we left off.
    resumed = await runner_mod.resume_build(
        "yes",
        deps,
        thread_id=outcome.thread_id,
        checkpointer=saver,
    )

    assert resumed.ok is True
    assert [o.task_id for o in resumed.outcomes] == ["a", "b"]
    assert resumed.pending_review is None
    # The retried task's prompt embeds the human answer — agents need
    # to know the question has been resolved.
    #
    # LangGraph's interrupt() re-executes the node from the top on
    # resume, so we see three task-a calls:
    #   1. pre-pause  — raises, except block calls interrupt() which
    #      raises GraphInterrupt and pauses.
    #   2. post-resume, first call inside the re-executed node — raises
    #      again (real agents don't know the question has been answered
    #      yet), except fires, interrupt() returns the resume value.
    #   3. post-resume, second call inside the except block — prompt
    #      now carries "Human review response: yes", agent returns OK.
    retry_prompts = [a["prompt"] for a in attempts if "models/a.py" in a["prompt"]]
    assert len(retry_prompts) == 3
    assert "Human review response" not in retry_prompts[0]
    assert "Human review response" not in retry_prompts[1]
    assert "Human review response: yes" in retry_prompts[2]


# ── Sqlite checkpointer (persistence across process boundaries) ───────


async def test_sqlite_checkpointer_persists_interrupt_across_reopens(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline value-prop of SqliteSaver: pause in one process,
    resume in another. We simulate "another process" by closing the
    saver's async ctx and re-opening the graph — the second open must
    find the paused build on the same thread_id.
    """
    call_count = {"n": 0}

    async def fake_inner(
        task_prompt: str,
        deps: CoderDeps,
        *,
        validator_targets: list[str] | None = None,
        max_attempts: int = 3,
        agent: Any = None,
    ) -> ValidatorLoopResult:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise HumanReviewRequired(question="Proceed?", options=["y", "n"])
        return _ok_result()

    monkeypatch.setattr(graph_mod, "run_task_with_validators", fake_inner)

    plan = _plan(_op("only"))
    deps = CoderDeps(workspace_root=workspace, project_id="persist-test")

    # First process: pause.
    outcome = await runner_mod.run_build_plan(
        plan, deps, checkpointer_kind="sqlite"
    )
    assert outcome.pending_review is not None
    assert outcome.pending_review.question == "Proceed?"
    thread_id = outcome.thread_id

    # Second process: open the checkpointer from scratch, resume.
    resumed = await runner_mod.resume_build(
        "y",
        deps,
        thread_id=thread_id,
        checkpointer_kind="sqlite",
    )
    assert resumed.ok is True
    assert len(resumed.outcomes) == 1
    assert resumed.outcomes[0].task_id == "only"
    assert call_count["n"] == 2, (
        f"inner loop should have been called twice (pause + resume), got {call_count['n']}"
    )
