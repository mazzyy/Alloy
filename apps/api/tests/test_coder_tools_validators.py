"""Unit tests for `app.agents.coder.tools.validators`.

We mock `run_command` to return canned tool output and assert:

* `run_validators` dispatches the right binary+args per target.
* Per-tool output parsers extract the right `ValidatorIssue` shape.
* `ok=True` iff every command exited 0.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import app.agents.coder.tools.validators as validators_mod
from app.agents.coder.context import CoderDeps
from app.agents.coder.results import CommandResult
from app.agents.coder.tools.validators import (
    _parse_eslint_json,
    _parse_mypy,
    _parse_pytest,
    _parse_ruff,
    _parse_tsc,
    _parse_vitest_json,
    run_validators,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


def _deps(workspace: Path) -> CoderDeps:
    return CoderDeps(workspace_root=workspace, turn_id="t", project_id="p")


# ── Parsers ────────────────────────────────────────────────────────────


def test_parse_ruff_extracts_code_and_location() -> None:
    out = (
        "apps/api/app/main.py:10:5: F401 `os` imported but unused\n"
        "apps/api/app/main.py:12:1: E302 expected 2 blank lines, found 1\n"
    )
    issues = _parse_ruff(out)
    assert len(issues) == 2
    assert issues[0].code == "F401"
    assert issues[0].line == 10
    assert issues[1].code == "E302"


def test_parse_mypy_with_and_without_code() -> None:
    out = (
        "app/main.py:3: error: Incompatible types [assignment]\n"
        "app/main.py:4: error: Untyped function\n"
    )
    issues = _parse_mypy(out)
    assert len(issues) == 2
    assert issues[0].code == "assignment"
    assert issues[1].code is None


def test_parse_pytest_failures() -> None:
    out = "FAILED tests/test_foo.py::test_bar - AssertionError: nope\n"
    err = "ERROR tests/test_boot.py\n"
    issues = _parse_pytest(out, err)
    targets = [i.path for i in issues]
    assert "tests/test_foo.py::test_bar" in targets
    assert "tests/test_boot.py" in targets


def test_parse_tsc_matches_error_lines() -> None:
    out = (
        "src/App.tsx(14,10): error TS2304: Cannot find name 'Foo'.\n"
        "src/App.tsx(20,1): error TS2322: Type 'string' is not assignable...\n"
    )
    issues = _parse_tsc(out)
    assert [i.code for i in issues] == ["TS2304", "TS2322"]


def test_parse_eslint_only_keeps_errors() -> None:
    payload = json.dumps(
        [
            {
                "filePath": "/app/src/A.tsx",
                "messages": [
                    {"severity": 2, "ruleId": "no-undef", "line": 3, "message": "oops"},
                    {"severity": 1, "ruleId": "prefer-const", "line": 4, "message": "warn"},
                ],
            }
        ]
    )
    issues = _parse_eslint_json(payload)
    assert len(issues) == 1
    assert issues[0].code == "no-undef"


def test_parse_vitest_failed_tests() -> None:
    payload = json.dumps(
        {
            "testResults": [
                {
                    "name": "src/App.test.tsx",
                    "assertionResults": [
                        {"status": "passed", "failureMessages": []},
                        {"status": "failed", "failureMessages": ["expected a === b"]},
                    ],
                }
            ]
        }
    )
    issues = _parse_vitest_json(payload)
    assert len(issues) == 1
    assert "expected a === b" in issues[0].message


# ── Orchestration ──────────────────────────────────────────────────────


async def _fake_runner(scripts: list[CommandResult]) -> Any:
    """Returns an async fn we can drop into the module as `run_command`."""
    queue = list(scripts)

    async def runner(_deps: CoderDeps, binary: str, args: list[str], *, timeout_s: int = 60):
        if not queue:
            return CommandResult(
                command=f"{binary} {' '.join(args)}",
                returncode=0,
                stdout="",
                stderr="",
                duration_s=0.0,
            )
        result = queue.pop(0)
        return result

    return runner


async def test_run_validators_python_target_dispatches_ruff_and_mypy(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripts = [
        CommandResult(
            command="ruff check",
            returncode=0,
            stdout="",
            stderr="",
            duration_s=0.1,
        ),
        CommandResult(
            command="mypy app",
            returncode=0,
            stdout="Success: no issues found\n",
            stderr="",
            duration_s=0.5,
        ),
    ]
    monkeypatch.setattr(validators_mod, "run_command", await _fake_runner(scripts))

    report = await run_validators(_deps(workspace), targets=["python"])
    assert report.ok is True
    assert report.issue_count == 0
    assert len(report.commands) == 2


async def test_run_validators_reports_issues_on_failure(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripts = [
        CommandResult(
            command="ruff check",
            returncode=1,
            stdout="apps/api/app/main.py:1:1: F401 `os` imported but unused\n",
            stderr="",
            duration_s=0.1,
        ),
        CommandResult(
            command="mypy app",
            returncode=1,
            stdout="app/main.py:3: error: Name 'x' is not defined [name-defined]\n",
            stderr="",
            duration_s=0.4,
        ),
    ]
    monkeypatch.setattr(validators_mod, "run_command", await _fake_runner(scripts))

    report = await run_validators(_deps(workspace), targets=["python"])
    assert report.ok is False
    # 1 ruff + 1 mypy.
    assert report.issue_count == 2
    tools = {i.tool for i in report.issues}
    assert tools == {"ruff", "mypy"}


async def test_run_validators_rejects_unknown_target(workspace: Path) -> None:
    with pytest.raises(Exception, match="unknown validator target"):
        await run_validators(_deps(workspace), targets=["not-a-target"])


# ── Scoping by touched paths ───────────────────────────────────────────
#
# Regression for an Azure run in which the Coder Agent added a clean
# User model but ruff reported 50 pre-existing lint issues in unrelated
# files (alembic/env.py, coder/tools/codegen.py, …), masking the clean
# addition and derailing the agent into lint-chasing across the repo.
# After the fix, `run_validators(paths=[...])` must:
#   (a) pass those paths as ruff/mypy positional args;
#   (b) drop the binary's default whole-repo token (`.` for ruff,
#       `app` for mypy);
#   (c) split by extension so a `.ts` file never reaches ruff; and
#   (d) skip ruff/mypy entirely when no Python files were touched.


async def test_run_validators_scopes_ruff_and_mypy_to_touched_py_files(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocations: list[tuple[str, list[str]]] = []

    async def recording_runner(
        _deps: CoderDeps, binary: str, args: list[str], *, timeout_s: int = 60
    ) -> CommandResult:
        invocations.append((binary, list(args)))
        return CommandResult(
            command=f"{binary} {' '.join(args)}",
            returncode=0,
            stdout="",
            stderr="",
            duration_s=0.0,
        )

    monkeypatch.setattr(validators_mod, "run_command", recording_runner)

    report = await run_validators(
        _deps(workspace),
        targets=["python"],
        paths=["apps/api/app/models/user.py", "apps/api/app/models/__init__.py"],
    )

    assert report.ok is True
    # `_pre_validator_autofix` runs ruff twice up front (format + check
    # --fix) on the touched paths. We only want to assert against the
    # actual validator invocation here — the one that uses `--output-
    # format` and feeds parsed issues back into the report. Filter the
    # autofix calls out by looking for the validator-specific flag.
    validator_ruff_calls = [
        args
        for b, args in invocations
        if b == "ruff" and "--output-format" in args
    ]
    assert validator_ruff_calls, (
        f"no validator ruff invocation in {invocations!r} — pre-autofix "
        f"may have suppressed it"
    )
    ruff_args = validator_ruff_calls[0]
    assert "." not in ruff_args
    assert "apps/api/app/models/user.py" in ruff_args
    assert "apps/api/app/models/__init__.py" in ruff_args
    # mypy invocation: default ended in `app`; that should be replaced.
    mypy_args = next(args for b, args in invocations if b == "mypy")
    assert "app" not in mypy_args
    assert "apps/api/app/models/user.py" in mypy_args


async def test_run_validators_skips_ruff_when_no_py_files_touched(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Touching only frontend files should keep ruff silent — it's the
    mechanism that prevents the whole-repo lint sweep on every attempt."""
    invocations: list[str] = []

    async def recording_runner(
        _deps: CoderDeps, binary: str, args: list[str], *, timeout_s: int = 60
    ) -> CommandResult:
        invocations.append(binary)
        return CommandResult(
            command=f"{binary} {' '.join(args)}",
            returncode=0,
            stdout="",
            stderr="",
            duration_s=0.0,
        )

    monkeypatch.setattr(validators_mod, "run_command", recording_runner)

    report = await run_validators(
        _deps(workspace),
        targets=["python"],
        paths=["apps/web/src/App.tsx"],
    )

    assert report.ok is True
    # No Python files touched → ruff and mypy both skipped.
    assert invocations == []


async def test_run_validators_whole_repo_when_paths_is_none(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Back-compat: passing `paths=None` keeps the old whole-repo mode
    so callers (and our own test suite) can still drive validators
    without a touched-set."""
    invocations: list[tuple[str, list[str]]] = []

    async def recording_runner(
        _deps: CoderDeps, binary: str, args: list[str], *, timeout_s: int = 60
    ) -> CommandResult:
        invocations.append((binary, list(args)))
        return CommandResult(
            command=f"{binary} {' '.join(args)}",
            returncode=0,
            stdout="",
            stderr="",
            duration_s=0.0,
        )

    monkeypatch.setattr(validators_mod, "run_command", recording_runner)

    await run_validators(_deps(workspace), targets=["python"])
    binaries = [b for b, _ in invocations]
    assert binaries == ["ruff", "mypy"]
    # ruff still gets the default whole-repo marker.
    assert invocations[0][1][-1] == "."
    # mypy still gets the default "app" positional.
    assert invocations[1][1][-1] == "app"


async def test_run_validators_mypy_passes_ignore_missing_imports(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """9th-regression: third-party deps without type stubs (pwdlib,
    argon2, bcrypt, ...) used to fail mypy with `import-not-found` and
    block the validator loop even though the runtime code was correct.

    The fix is two-pronged: the template's `pyproject.toml` sets
    `[tool.mypy] ignore_missing_imports = true`, AND the validator's
    mypy invocation passes `--ignore-missing-imports` as a defensive
    second layer for projects that lose the override. Pin the flag
    here so a future refactor can't silently strip it.
    """
    invocations: list[tuple[str, list[str]]] = []

    async def recording_runner(
        _deps: CoderDeps, binary: str, args: list[str], *, timeout_s: int = 60
    ) -> CommandResult:
        invocations.append((binary, list(args)))
        return CommandResult(
            command=f"{binary} {' '.join(args)}",
            returncode=0,
            stdout="",
            stderr="",
            duration_s=0.0,
        )

    monkeypatch.setattr(validators_mod, "run_command", recording_runner)

    # Whole-repo mode — no path scoping should affect the flag.
    await run_validators(_deps(workspace), targets=["python"])
    mypy_args_whole = next(args for b, args in invocations if b == "mypy")
    assert "--ignore-missing-imports" in mypy_args_whole, (
        f"expected --ignore-missing-imports in {mypy_args_whole!r}"
    )

    # Scoped mode — flag must survive the path-rewrite.
    invocations.clear()
    await run_validators(
        _deps(workspace),
        targets=["python"],
        paths=["backend/app/core/security.py"],
    )
    mypy_args_scoped = next(args for b, args in invocations if b == "mypy")
    assert "--ignore-missing-imports" in mypy_args_scoped, (
        f"expected --ignore-missing-imports in scoped {mypy_args_scoped!r}"
    )
    # The path replacement must still have happened.
    assert "backend/app/core/security.py" in mypy_args_scoped
    assert "app" not in mypy_args_scoped


# 8th-regression — pre-validator auto-fix pass. Trivially-auto-fixable
# lint codes (W291 trailing whitespace from the alembic Mako template,
# I001 import-order on edits to scaffold files like `pwdlib.py`) used to
# cost the agent an entire validator-loop attempt. The fix runs `ruff
# format` + `ruff check --fix --select W,I,...` against touched Python
# paths BEFORE the validator's actual `ruff check`. These tests pin the
# call sequence and selector against future regressions.


async def test_run_validators_runs_pre_validator_autofix_on_touched_py(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocations: list[tuple[str, list[str]]] = []

    async def recording_runner(
        _deps: CoderDeps, binary: str, args: list[str], *, timeout_s: int = 60
    ) -> CommandResult:
        invocations.append((binary, list(args)))
        return CommandResult(
            command=f"{binary} {' '.join(args)}",
            returncode=0,
            stdout="",
            stderr="",
            duration_s=0.0,
        )

    monkeypatch.setattr(validators_mod, "run_command", recording_runner)

    paths = [
        "backend/app/alembic/versions/abc123_add_task.py",
        "backend/app/models.py",
    ]
    await run_validators(_deps(workspace), targets=["python"], paths=paths)

    # First two calls must be the autofix pass.
    assert len(invocations) >= 2
    first_binary, first_args = invocations[0]
    assert first_binary == "ruff"
    assert first_args[0] == "format"
    # `format` call must target every touched Python path.
    for p in paths:
        assert p in first_args, f"ruff format missing {p}: {first_args!r}"

    second_binary, second_args = invocations[1]
    assert second_binary == "ruff"
    assert second_args[:2] == ["check", "--fix"]
    assert "--select" in second_args
    selector_idx = second_args.index("--select")
    selector = second_args[selector_idx + 1]
    # The selector must include the codes we know we want to absorb
    # silently — W291 (alembic Mako trailing whitespace), I001 (import
    # order on scaffold edits), W292/W293 (blank-line whitespace).
    for code in ("W291", "W292", "W293", "I001"):
        assert code in selector, f"autofix selector missing {code}: {selector!r}"
    for p in paths:
        assert p in second_args, f"ruff check --fix missing {p}: {second_args!r}"


async def test_run_validators_pre_autofix_skips_when_no_py_paths(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Front-end-only touched paths must NOT trigger ruff at all — the
    autofix is Python-only by design."""
    invocations: list[str] = []

    async def recording_runner(
        _deps: CoderDeps, binary: str, args: list[str], *, timeout_s: int = 60
    ) -> CommandResult:
        invocations.append(binary)
        return CommandResult(
            command=f"{binary} {' '.join(args)}",
            returncode=0,
            stdout="",
            stderr="",
            duration_s=0.0,
        )

    monkeypatch.setattr(validators_mod, "run_command", recording_runner)

    await run_validators(
        _deps(workspace),
        targets=["python"],
        paths=["frontend/src/App.tsx", "frontend/src/main.ts"],
    )

    # No Python paths touched → no ruff invocations at all (autofix
    # skipped, validator ruff also skipped via the existing scope).
    assert "ruff" not in invocations
