"""Unit tests for `app.agents.coder.tools.commands`.

Two axes to cover:

1. Allow-list discipline — non-whitelisted binaries, paths, and empty
   strings all raise.
2. Sandbox routing — when `CoderDeps.sandbox` is set, the command goes
   through `sandbox.exec()` (we use a fake manager). When it isn't,
   the command falls back to a local subprocess.

We don't shell out to a real `ruff` in CI — we use a fake sandbox that
records every `exec` call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.agents.coder.context import CoderDeps
from app.agents.coder.errors import DisallowedCommandError, ToolInputError
from app.agents.coder.tools.commands import ALLOWED_BINARIES, run_command


# ── Fakes ──────────────────────────────────────────────────────────────


@dataclass
class _FakeCall:
    handle: Any
    service: str
    cmd: list[str]
    timeout_s: int


@dataclass
class _FakeSandbox:
    calls: list[_FakeCall] = field(default_factory=list)
    scripted: tuple[int, str, str] = (0, "ok\n", "")

    async def exec(
        self,
        handle: Any,
        service: str,
        cmd: list[str],
        *,
        timeout_s: int = 120,
    ) -> tuple[int, str, str]:
        self.calls.append(_FakeCall(handle, service, cmd, timeout_s))
        return self.scripted


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / ".alloy").mkdir()
    return tmp_path


def _deps(workspace: Path, sandbox: Any | None = None) -> CoderDeps:
    return CoderDeps(
        workspace_root=workspace,
        sandbox=sandbox,
        sandbox_handle=object() if sandbox else None,
        turn_id="turn-1",
        project_id=str(uuid4()),
    )


# ── Allow-list ─────────────────────────────────────────────────────────


async def test_run_command_rejects_unlisted_binary(workspace: Path) -> None:
    with pytest.raises(DisallowedCommandError):
        await run_command(_deps(workspace), "curl", ["https://example.com"])


async def test_run_command_rejects_path_not_bare_name(workspace: Path) -> None:
    with pytest.raises(ToolInputError, match="bare binary name"):
        await run_command(_deps(workspace), "/usr/bin/ruff", ["check"])


async def test_run_command_rejects_empty_binary(workspace: Path) -> None:
    with pytest.raises(ToolInputError, match="non-empty"):
        await run_command(_deps(workspace), "  ", [])


async def test_run_command_rejects_nonstring_args(workspace: Path) -> None:
    with pytest.raises(ToolInputError, match="must all be strings"):
        # mypy-ignore: deliberately wrong type to exercise runtime guard.
        await run_command(_deps(workspace), "ruff", [123])  # type: ignore[list-item]


def test_allow_list_shape_is_stable() -> None:
    """Smoke test — the roadmap §3 list must be in ALLOWED_BINARIES."""
    must_include = {"ruff", "mypy", "pytest", "alembic", "uv", "npm", "tsc", "eslint", "vitest"}
    assert must_include.issubset(ALLOWED_BINARIES)


# ── Routing ────────────────────────────────────────────────────────────


async def test_run_command_routes_python_tools_to_backend(workspace: Path) -> None:
    sandbox = _FakeSandbox(scripted=(0, "ok\n", ""))
    deps = _deps(workspace, sandbox=sandbox)
    result = await run_command(deps, "pytest", ["-x"])
    assert sandbox.calls, "sandbox.exec should have been called"
    assert sandbox.calls[0].service == "backend"
    assert sandbox.calls[0].cmd == ["pytest", "-x"]
    assert result.returncode == 0
    assert "pytest -x" in result.command


async def test_run_command_routes_frontend_tools_to_frontend(workspace: Path) -> None:
    sandbox = _FakeSandbox(scripted=(1, "", "eslint failed\n"))
    deps = _deps(workspace, sandbox=sandbox)
    result = await run_command(deps, "eslint", ["."])
    assert sandbox.calls[0].service == "frontend"
    assert result.returncode == 1
    assert "eslint failed" in result.stderr


async def test_run_command_captures_and_truncates_large_stdout(workspace: Path) -> None:
    big = "x" * (20 * 1024)
    sandbox = _FakeSandbox(scripted=(0, big, ""))
    deps = _deps(workspace, sandbox=sandbox)
    result = await run_command(deps, "ruff", ["check"])
    assert result.truncated is True
    # 8 KB cap ⇒ exactly 8*1024 chars kept.
    assert len(result.stdout) == 8 * 1024


async def test_run_command_fallback_subprocess_unknown_binary_returns_127(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no sandbox is attached, the subprocess path runs. We patch
    `shutil.which` to simulate the binary being missing."""
    import app.agents.coder.tools.commands as cmd_mod

    monkeypatch.setattr(cmd_mod.shutil, "which", lambda _: None)
    # `uv` is on the allow-list so it passes validation — the missing
    # binary is caught at the subprocess layer.
    result = await run_command(_deps(workspace), "uv", ["--version"])
    assert result.returncode == 127
    assert "not found on PATH" in result.stderr
