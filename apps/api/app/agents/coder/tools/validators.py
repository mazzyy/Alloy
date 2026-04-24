"""`run_validators` — parallelise lint/type/test across the scaffold.

Roadmap §3: after each write the validator runs `ruff check --fix`,
`mypy`, and `pytest -x` (Python) plus `tsc --noEmit`, `eslint`, `vitest
run` (frontend), with the top-N diagnostics fed back in as "fix these
specific errors; do not refactor unrelated code."

Design:

* Validators are *declared*, not hardcoded — `TARGETS` maps a shorthand
  the agent can use (`"python"`, `"frontend"`, `"python-tests"`, ...)
  to a list of shell commands.
* Each command runs via `tools.commands.run_command`, so it picks up
  the sandbox path if one is attached.
* We parse each tool's native output format into `ValidatorIssue`s:
    - ruff: `file:line:col: CODE message`
    - mypy: `file:line: error: message [code]`
    - pytest: short-form FAILED/ERROR lines + captured stdout excerpt
    - tsc: `file(line,col): error TSxxxx: message`
    - eslint: JSON formatter
    - vitest: JSON reporter
* The report's `ok` flag is `all(cmd.returncode == 0)`. We return every
  command's raw output alongside the parsed issues so the agent can
  debug unusual failures.

Truncation: we cap at 50 parsed issues in the report. Full logs stay
in `ValidatorReport.commands[*].stdout/stderr`.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from pydantic_ai import RunContext

from app.agents.coder.context import CoderDeps
from app.agents.coder.errors import ToolInputError
from app.agents.coder.results import CommandResult, ValidatorIssue, ValidatorReport
from app.agents.coder.tools.commands import run_command

if TYPE_CHECKING:
    from pydantic_ai import Agent


_ISSUE_CAP = 50


# Shorthand → list of (binary, args). We build this as a mapping so the
# agent sees a consistent menu in the tool docstring.
TARGETS: dict[str, list[tuple[str, list[str]]]] = {
    "python": [
        ("ruff", ["check", "--output-format", "concise", "."]),
        ("mypy", ["--no-color-output", "--hide-error-context", "app"]),
    ],
    "python-tests": [
        ("pytest", ["-x", "--tb=short", "-q"]),
    ],
    "frontend": [
        ("tsc", ["--noEmit"]),
        ("eslint", ["--format", "json", "."]),
    ],
    "frontend-tests": [
        ("vitest", ["run", "--reporter=json"]),
    ],
}


# ── Parsers ────────────────────────────────────────────────────────────

_RUFF_RE = re.compile(
    r"^(?P<path>[^:\n]+):(?P<line>\d+):(?P<col>\d+):\s+(?P<code>[A-Z0-9]+)\s+(?P<msg>.*)$"
)


def _parse_ruff(out: str) -> list[ValidatorIssue]:
    issues: list[ValidatorIssue] = []
    for line in out.splitlines():
        m = _RUFF_RE.match(line.rstrip())
        if not m:
            continue
        issues.append(
            ValidatorIssue(
                tool="ruff",
                path=m.group("path"),
                line=int(m.group("line")),
                code=m.group("code"),
                message=m.group("msg"),
            )
        )
    return issues


_MYPY_RE = re.compile(
    r"^(?P<path>[^:\n]+):(?P<line>\d+):\s+error:\s+(?P<msg>.*?)(?:\s+\[(?P<code>[\w-]+)\])?$"
)


def _parse_mypy(out: str) -> list[ValidatorIssue]:
    issues: list[ValidatorIssue] = []
    for line in out.splitlines():
        m = _MYPY_RE.match(line.rstrip())
        if not m:
            continue
        issues.append(
            ValidatorIssue(
                tool="mypy",
                path=m.group("path"),
                line=int(m.group("line")),
                code=m.group("code"),
                message=m.group("msg"),
            )
        )
    return issues


_PYTEST_FAIL_RE = re.compile(r"^(?:FAILED|ERROR)\s+(?P<target>\S+)(?:\s+-\s+(?P<msg>.*))?$")


def _parse_pytest(out: str, err: str) -> list[ValidatorIssue]:
    issues: list[ValidatorIssue] = []
    for line in (out + "\n" + err).splitlines():
        m = _PYTEST_FAIL_RE.match(line.strip())
        if not m:
            continue
        issues.append(
            ValidatorIssue(
                tool="pytest",
                path=m.group("target"),
                message=m.group("msg") or "test failed",
            )
        )
    return issues


_TSC_RE = re.compile(
    r"^(?P<path>[^(\n]+)\((?P<line>\d+),\d+\):\s+error\s+(?P<code>TS\d+):\s+(?P<msg>.*)$"
)


def _parse_tsc(out: str) -> list[ValidatorIssue]:
    issues: list[ValidatorIssue] = []
    for line in out.splitlines():
        m = _TSC_RE.match(line.rstrip())
        if not m:
            continue
        issues.append(
            ValidatorIssue(
                tool="tsc",
                path=m.group("path"),
                line=int(m.group("line")),
                code=m.group("code"),
                message=m.group("msg"),
            )
        )
    return issues


def _parse_eslint_json(out: str) -> list[ValidatorIssue]:
    issues: list[ValidatorIssue] = []
    try:
        data = json.loads(out or "[]")
    except ValueError:
        return issues
    if not isinstance(data, list):
        return issues
    for file_entry in data:
        if not isinstance(file_entry, dict):
            continue
        path = str(file_entry.get("filePath", "") or "")
        for msg in file_entry.get("messages", []) or []:
            if msg.get("severity", 0) < 2:
                # 1 = warn, 2 = error; we care about errors only.
                continue
            issues.append(
                ValidatorIssue(
                    tool="eslint",
                    path=path or None,
                    line=int(msg.get("line") or 0) or None,
                    code=str(msg.get("ruleId") or "") or None,
                    message=str(msg.get("message") or ""),
                )
            )
    return issues


def _parse_vitest_json(out: str) -> list[ValidatorIssue]:
    issues: list[ValidatorIssue] = []
    try:
        data = json.loads(out or "{}")
    except ValueError:
        return issues
    for file_entry in data.get("testResults", []) or []:
        path = str(file_entry.get("name", "") or "")
        for test in file_entry.get("assertionResults", []) or []:
            if test.get("status") != "failed":
                continue
            msg = " | ".join(test.get("failureMessages") or []) or "test failed"
            issues.append(
                ValidatorIssue(
                    tool="vitest",
                    path=path or None,
                    message=msg[:500],
                )
            )
    return issues


def _parse_for(binary: str, cmd: CommandResult) -> list[ValidatorIssue]:
    if binary == "ruff":
        return _parse_ruff(cmd.stdout + "\n" + cmd.stderr)
    if binary == "mypy":
        return _parse_mypy(cmd.stdout)
    if binary == "pytest":
        return _parse_pytest(cmd.stdout, cmd.stderr)
    if binary == "tsc":
        return _parse_tsc(cmd.stdout + "\n" + cmd.stderr)
    if binary == "eslint":
        return _parse_eslint_json(cmd.stdout)
    if binary == "vitest":
        return _parse_vitest_json(cmd.stdout)
    return []


# ── Orchestration ─────────────────────────────────────────────────────


async def run_validators(
    deps: "CoderDeps",
    targets: list[str] | None = None,
) -> ValidatorReport:
    """Run validators for each named target sequentially and collect issues.

    Sequentially, not in parallel (yet) — LiteLLM routing + sandbox
    contention make parallel stderr interleaving unreadable for now.
    The issue count is a stronger signal than wall-clock for this tool.
    """
    selected = targets or ["python"]
    for t in selected:
        if t not in TARGETS:
            raise ToolInputError(
                f"unknown validator target {t!r}; choose from {sorted(TARGETS)}"
            )

    commands: list[CommandResult] = []
    issues: list[ValidatorIssue] = []
    overall_ok = True
    for target in selected:
        for binary, args in TARGETS[target]:
            result = await run_command(deps, binary, args, timeout_s=120)
            commands.append(result)
            if result.returncode != 0:
                overall_ok = False
            parsed = _parse_for(binary, result)
            issues.extend(parsed)
            if len(issues) >= _ISSUE_CAP:
                issues = issues[:_ISSUE_CAP]
                break
        if len(issues) >= _ISSUE_CAP:
            break

    return ValidatorReport(
        ok=overall_ok,
        issue_count=len(issues),
        issues=issues,
        commands=commands,
    )


def register(agent: "Agent[CoderDeps, str]") -> None:
    @agent.tool
    async def run_validators_tool(
        ctx: "RunContext[CoderDeps]",
        targets: list[str] | None = None,
    ) -> ValidatorReport:
        """Run one or more validator suites and return the top issues.

        Valid targets: `python` (ruff + mypy), `python-tests` (pytest),
        `frontend` (tsc + eslint), `frontend-tests` (vitest).
        Defaults to `["python"]` if omitted.

        On any failing command `ok=False` and `issues` contains parsed
        top-N diagnostics. Full raw output is available in
        `commands[*].stdout/stderr`.
        """
        return await run_validators(ctx.deps, targets)
