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
