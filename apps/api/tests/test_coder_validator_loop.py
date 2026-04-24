"""Tests for `run_task_with_validators` — the Coder Agent + validator retry loop.

We script the Coder Agent via pydantic-ai's `FunctionModel` so we don't
need Azure creds, and we patch `run_validators` at the module the loop
imports from so the loop sees a deterministic report sequence. Every
test asserts both the final `ValidatorLoopResult` and the per-attempt
history — getting only the headline result right while silently
mangling intermediate state is the class of bug we've been burned by
elsewhere in the agent stack.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.agents.coder import validator_loop as loop_mod
from app.agents.coder.context import CoderDeps
from app.agents.coder.errors import HumanReviewRequired
from app.agents.coder.results import (
    CommandResult,
    ValidatorIssue,
    ValidatorReport,
)
from app.agents.coder.tools import register_tools


pytestmark = pytest.mark.asyncio


# ── Test helpers ───────────────────────────────────────────────────────


def _make_report(*, ok: bool, issues: int = 0) -> ValidatorReport:
    """Build a minimal ValidatorReport. Details beyond ok/count don't
    matter for the loop's branching logic; the tests assert on the
    aggregate behaviour, not every field.
    """
    issue_list = [
        ValidatorIssue(
            tool="ruff",
            path="app/models.py",
            line=10 + i,
            code="E501",
            message=f"line too long ({i})",
        )
        for i in range(issues)
    ]
    return ValidatorReport(
        ok=ok,
        issue_count=len(issue_list),
        issues=issue_list,
        commands=[
            CommandResult(
                command="ruff check .",
                returncode=0 if ok else 1,
                stdout="",
                stderr="",
                duration_s=0.01,
            )
        ],
    )


def _scripted_agent(outputs: list[str]) -> Agent[CoderDeps, str]:
    """Return a Coder Agent whose model emits one final string per turn.

    We register all real tools so the schema matches production, but the
    scripted model never *calls* tools — it only returns the final text.
    The validator loop cares about `result.output` + `result.all_messages()`,
    both of which work fine without tool calls.
    """
    idx = [0]

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        i = idx[0]
        idx[0] += 1
        text = outputs[i] if i < len(outputs) else outputs[-1]
        return ModelResponse(parts=[TextPart(content=text)])

    agent = Agent[CoderDeps, str](
        model=FunctionModel(respond),
        deps_type=CoderDeps,
        output_type=str,
        system_prompt="test-coder",
        retries=1,
    )
    register_tools(agent)
    return agent


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "apps" / "api" / "app").mkdir(parents=True)
    (tmp_path / "apps" / "api" / "app" / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )
    return tmp_path


# ── The loop's happy path and failure path ────────────────────────────


async def test_first_attempt_green_returns_ok_with_one_attempt(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If validators pass on attempt 1, we don't run a second attempt."""

    async def fake_validators(
        deps: CoderDeps,
        targets: list[str],
        *,
        paths: list[str] | None = None,
    ) -> ValidatorReport:
        return _make_report(ok=True)

    monkeypatch.setattr(loop_mod, "run_validators", fake_validators)

    agent = _scripted_agent(["Wrote the User model."])
    deps = CoderDeps(workspace_root=workspace, turn_id="t", project_id="p")

    result = await loop_mod.run_task_with_validators(
        "Add a User model",
        deps,
        validator_targets=["python"],
        max_attempts=3,
        agent=agent,
    )

    assert result.ok is True
    assert result.attempts_used == 1
    assert result.max_attempts == 3
    assert len(result.attempts) == 1
    assert result.attempts[0].agent_error is None
    assert result.attempts[0].report is not None
    assert result.attempts[0].report.ok is True
    assert result.final_report is not None and result.final_report.ok is True


async def test_fails_then_succeeds_returns_ok_with_two_attempts(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attempt 1 fails, attempt 2 passes — loop stops at attempt 2."""
    reports = iter([_make_report(ok=False, issues=2), _make_report(ok=True)])

    async def fake_validators(
        deps: CoderDeps,
        targets: list[str],
        *,
        paths: list[str] | None = None,
    ) -> ValidatorReport:
        return next(reports)

    monkeypatch.setattr(loop_mod, "run_validators", fake_validators)

    agent = _scripted_agent(["first attempt", "second attempt"])
    deps = CoderDeps(workspace_root=workspace)

    result = await loop_mod.run_task_with_validators(
        "Add a User model",
        deps,
        max_attempts=3,
        agent=agent,
    )

    assert result.ok is True
    assert result.attempts_used == 2
    assert len(result.attempts) == 2
    assert result.attempts[0].report is not None and result.attempts[0].report.ok is False
    assert result.attempts[1].report is not None and result.attempts[1].report.ok is True
    # Final outputs reflect the agent's second turn.
    assert "second" in result.attempts[1].agent_output


async def test_always_failing_validators_exhaust_attempts(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the agent never makes validators happy we stop at max_attempts
    and return ok=False with every attempt's report preserved.
    """

    async def fake_validators(
        deps: CoderDeps,
        targets: list[str],
        *,
        paths: list[str] | None = None,
    ) -> ValidatorReport:
        return _make_report(ok=False, issues=3)

    monkeypatch.setattr(loop_mod, "run_validators", fake_validators)

    agent = _scripted_agent(["try one", "try two", "try three"])
    deps = CoderDeps(workspace_root=workspace)

    result = await loop_mod.run_task_with_validators(
        "Impossible task",
        deps,
        max_attempts=3,
        agent=agent,
    )

    assert result.ok is False
    assert result.attempts_used == 3
    assert len(result.attempts) == 3
    for a in result.attempts:
        assert a.report is not None and a.report.ok is False
    assert result.final_report is not None and result.final_report.issue_count == 3


# ── Agent-error path ───────────────────────────────────────────────────


async def test_agent_error_on_first_attempt_is_recorded_and_retried(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the agent raises, we record the error and retry. If the retry
    runs cleanly and passes validators, `ok=True`."""

    calls = [0]

    async def fake_validators(
        deps: CoderDeps,
        targets: list[str],
        *,
        paths: list[str] | None = None,
    ) -> ValidatorReport:
        calls[0] += 1
        return _make_report(ok=True)

    monkeypatch.setattr(loop_mod, "run_validators", fake_validators)

    # First FunctionModel call raises; second returns a normal message.
    first = [True]

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if first[0]:
            first[0] = False
            raise RuntimeError("boom")
        return ModelResponse(parts=[TextPart(content="recovered")])

    agent = Agent[CoderDeps, str](
        model=FunctionModel(respond),
        deps_type=CoderDeps,
        output_type=str,
        system_prompt="test-coder",
        retries=1,
    )
    register_tools(agent)
    deps = CoderDeps(workspace_root=workspace)

    result = await loop_mod.run_task_with_validators(
        "task",
        deps,
        max_attempts=3,
        agent=agent,
    )

    assert result.ok is True
    assert result.attempts_used == 2
    # Attempt 1 has the error, no report; attempt 2 is clean.
    assert result.attempts[0].agent_error is not None
    assert "boom" in result.attempts[0].agent_error
    assert result.attempts[0].report is None
    assert result.attempts[1].agent_error is None
    assert result.attempts[1].report is not None
    # Validators only ran once (for the successful attempt).
    assert calls[0] == 1


async def test_human_review_required_propagates(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`HumanReviewRequired` must escape the loop immediately so the
    LangGraph outer loop can pause the build — we never want to burn
    retries on a question the agent explicitly flagged."""

    async def fake_validators(
        deps: CoderDeps,
        targets: list[str],
        *,
        paths: list[str] | None = None,
    ) -> ValidatorReport:
        return _make_report(ok=True)

    monkeypatch.setattr(loop_mod, "run_validators", fake_validators)

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise HumanReviewRequired(question="approve destructive op?", options=["yes", "no"])

    agent = Agent[CoderDeps, str](
        model=FunctionModel(respond),
        deps_type=CoderDeps,
        output_type=str,
        system_prompt="test-coder",
        retries=1,
    )
    register_tools(agent)
    deps = CoderDeps(workspace_root=workspace)

    with pytest.raises(HumanReviewRequired) as exc_info:
        await loop_mod.run_task_with_validators(
            "task that needs approval", deps, max_attempts=3, agent=agent
        )
    assert exc_info.value.question == "approve destructive op?"
    assert exc_info.value.options == ["yes", "no"]


# ── Input validation ───────────────────────────────────────────────────


async def test_max_attempts_must_be_at_least_one(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_validators(
        deps: CoderDeps,
        targets: list[str],
        *,
        paths: list[str] | None = None,
    ) -> ValidatorReport:
        return _make_report(ok=True)

    monkeypatch.setattr(loop_mod, "run_validators", fake_validators)

    agent = _scripted_agent(["done"])
    deps = CoderDeps(workspace_root=workspace)

    with pytest.raises(ValueError, match="max_attempts"):
        await loop_mod.run_task_with_validators(
            "task", deps, max_attempts=0, agent=agent
        )


# ── Retry-prompt formatting ────────────────────────────────────────────


def test_format_issue_renders_locationful_diagnostic() -> None:
    issue = ValidatorIssue(
        tool="ruff",
        path="app/models.py",
        line=42,
        code="E501",
        message="line too long",
    )
    line = loop_mod._format_issue(issue)
    assert line.startswith("- ")
    assert "ruff" in line
    assert "app/models.py:42" in line
    assert "E501" in line
    assert "line too long" in line


def test_format_issue_tolerates_missing_fields() -> None:
    issue = ValidatorIssue(tool="pytest", message="collection error")
    line = loop_mod._format_issue(issue)
    assert line == "- pytest — collection error"


def test_build_retry_prompt_contains_roadmap_exact_phrase() -> None:
    """The roadmap mandates the wording 'fix these specific errors; do
    not refactor unrelated code'. This test guards that verbatim — the
    phrase is part of the product contract with the Coder Agent's
    system prompt and changing it silently would alter behaviour."""
    report = _make_report(ok=False, issues=2)
    prompt = loop_mod._build_retry_prompt(report, attempt=2, max_attempts=3)
    assert "fix these specific errors" in prompt.lower()
    assert "do not refactor unrelated code" in prompt.lower()
    # And the attempt counter is included so the agent knows how much
    # rope it has left.
    assert "2/3" in prompt
    # Each issue shows up on its own line.
    assert prompt.count("\n- ") >= 2


def test_build_retry_prompt_truncates_long_issue_list() -> None:
    """When more than _TOP_K_ISSUES issues are reported we include only
    the first N and surface the "omitted" count."""
    report = _make_report(ok=False, issues=loop_mod._TOP_K_ISSUES + 5)
    prompt = loop_mod._build_retry_prompt(report, attempt=1, max_attempts=3)
    assert f"(+5 more issues omitted)" in prompt


# ── Validator scoping propagation ──────────────────────────────────────


async def test_loop_passes_touched_paths_to_validators(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the loop must forward `deps.touched_paths` to
    `run_validators(paths=...)`. Without this, pre-existing lint debt in
    unrelated files re-surfaces every attempt and the model goes
    lint-chasing. Observed against Azure, fixed by scoping validators
    per touched-set."""

    captured_paths: list[list[str] | None] = []

    async def fake_validators(
        deps: CoderDeps,
        targets: list[str],
        *,
        paths: list[str] | None = None,
    ) -> ValidatorReport:
        captured_paths.append(paths)
        return _make_report(ok=True)

    monkeypatch.setattr(loop_mod, "run_validators", fake_validators)

    agent = _scripted_agent(["added User model"])
    deps = CoderDeps(workspace_root=workspace)
    # Pretend the agent's tools recorded two writes during the run. The
    # loop reads this set *after* the agent runs, so seeding it
    # up-front simulates the post-tool state.
    deps.touched_paths.update(
        {"apps/api/app/models/user.py", "apps/api/app/models/__init__.py"}
    )

    result = await loop_mod.run_task_with_validators(
        "Add a User model",
        deps,
        max_attempts=1,
        agent=agent,
    )

    assert result.ok is True
    assert captured_paths == [
        sorted({"apps/api/app/models/user.py", "apps/api/app/models/__init__.py"})
    ]


async def test_loop_skips_validators_entirely_when_nothing_touched(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the agent made no writes, skip validators entirely and return a
    clean empty report.

    Earlier behavior ran validators whole-repo in this case on the theory
    that it "catches silent no-ops". That rationale turned out to be
    wrong: ruff/mypy/pytest measure code quality, not spec compliance —
    running them against the whole repo when the agent didn't write
    anything is guaranteed to re-surface the exact pre-existing lint
    debt we were trying to hide from the model in the first place.

    Spec-compliance checks (did the agent actually do the thing?) belong
    to the outer build loop (#24 LangGraph) or a dedicated assertion
    step, not to this loop. Observed against Azure: a run where `user.py`
    already existed, the agent read it, confirmed the fields, committed
    empty, and then the loop triggered a whole-repo lint sweep that
    turned up 50 unrelated issues and failed the task."""

    called = False

    async def fake_validators(
        deps: CoderDeps,
        targets: list[str],
        *,
        paths: list[str] | None = None,
    ) -> ValidatorReport:
        nonlocal called
        called = True
        return _make_report(ok=True)

    monkeypatch.setattr(loop_mod, "run_validators", fake_validators)

    agent = _scripted_agent(["nothing to do"])
    deps = CoderDeps(workspace_root=workspace)

    result = await loop_mod.run_task_with_validators(
        "already done", deps, max_attempts=1, agent=agent
    )
    assert called is False, "run_validators must not be invoked when nothing was touched"
    assert result.ok is True
    assert result.attempts_used == 1
    assert result.final_report is not None
    assert result.final_report.ok is True
    assert result.final_report.issue_count == 0
    assert result.final_report.commands == []
