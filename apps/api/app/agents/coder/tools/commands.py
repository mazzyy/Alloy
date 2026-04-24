"""`run_command` + the strict binary allow-list.

Roadmap §3: "`run_command(cmd)` with an allow-list (`alembic`, `pytest`,
`ruff`, `mypy`, `npm`, `tsc`, `eslint`, `vitest`, `uv`)." Nothing else.

Execution model: if a `SandboxManager` is attached via `CoderDeps`, the
command runs inside the sandbox's backend or frontend service
container (so tools see the same filesystem and venv the real app does).
If no manager is attached — the test and dev-loop path — it runs as a
plain subprocess with cwd pinned to the workspace root.

The allow-list is the *only* gate between the LLM and arbitrary shell
execution. Keep it tight. Anything that looks like it belongs on the
host (docker, kubectl, curl, rm, sudo) stays off the list forever.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic_ai import RunContext

from app.agents.coder.context import CoderDeps
from app.agents.coder.errors import DisallowedCommandError, ToolInputError
from app.agents.coder.results import CommandResult

if TYPE_CHECKING:
    from pydantic_ai import Agent


# The only binaries the Coder Agent may invoke. Exposed as a module
# constant so the validator module and tests can import it without
# duplicating the list.
ALLOWED_BINARIES: frozenset[str] = frozenset(
    {
        # Python toolchain
        "alembic",
        "pytest",
        "ruff",
        "mypy",
        "uv",
        "python",
        # Frontend toolchain — via the project's package manager.
        "npm",
        "npx",
        "pnpm",
        "tsc",
        "eslint",
        "vitest",
    }
)

# Per-service routing. `run_command` runs Python tools in the `backend`
# container and JS tools in the `frontend` container when a
# SandboxManager is present.
_BACKEND_TOOLS = frozenset({"alembic", "pytest", "ruff", "mypy", "uv", "python"})
_FRONTEND_TOOLS = frozenset({"npm", "npx", "pnpm", "tsc", "eslint", "vitest"})

# stdout/stderr truncation cap (per stream). Matches `CommandResult.truncated`.
_TRUNC = 8 * 1024


def _truncate(s: str) -> tuple[str, bool]:
    if len(s) <= _TRUNC:
        return s, False
    return s[-_TRUNC:], True


def _service_for(binary: str) -> str:
    if binary in _FRONTEND_TOOLS:
        return "frontend"
    return "backend"


async def _run_subprocess(
    binary: str,
    args: list[str],
    *,
    cwd: Path,
    timeout_s: int,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """Run a command as a local subprocess. The fallback path when no
    sandbox is attached (tests, early dev-loop).
    """
    resolved = shutil.which(binary)
    if not resolved:
        return CommandResult(
            command=f"{binary} {' '.join(args)}",
            returncode=127,
            stdout="",
            stderr=f"binary not found on PATH: {binary}",
            duration_s=0.0,
        )
    start = time.perf_counter()
    full_env = {**os.environ, **(env or {})}
    proc = await asyncio.create_subprocess_exec(
        resolved,
        *args,
        cwd=str(cwd),
        env=full_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        rc = proc.returncode if proc.returncode is not None else 1
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return CommandResult(
            command=f"{binary} {' '.join(args)}",
            returncode=124,
            stdout="",
            stderr=f"timed out after {timeout_s}s",
            duration_s=timeout_s * 1.0,
        )
    duration = time.perf_counter() - start
    stdout, out_trunc = _truncate(out_b.decode("utf-8", errors="replace"))
    stderr, err_trunc = _truncate(err_b.decode("utf-8", errors="replace"))
    return CommandResult(
        command=f"{binary} {' '.join(args)}",
        returncode=rc,
        stdout=stdout,
        stderr=stderr,
        truncated=out_trunc or err_trunc,
        duration_s=round(duration, 3),
    )


async def _run_in_sandbox(
    sandbox: Any,
    handle: Any,
    binary: str,
    args: list[str],
    *,
    timeout_s: int,
) -> CommandResult:
    """Round-trip through `SandboxManager.exec()`.

    The sandbox runs the command in the appropriate service (backend vs
    frontend), with the container's working directory set to `/app`.
    """
    service = _service_for(binary)
    start = time.perf_counter()
    rc, out, err = await sandbox.exec(
        handle,
        service,
        [binary, *args],
        timeout_s=timeout_s,
    )
    duration = time.perf_counter() - start
    stdout, out_trunc = _truncate(out)
    stderr, err_trunc = _truncate(err)
    return CommandResult(
        command=f"{binary} {' '.join(args)}",
        returncode=rc,
        stdout=stdout,
        stderr=stderr,
        truncated=out_trunc or err_trunc,
        duration_s=round(duration, 3),
    )


def _validate_binary(binary: str) -> None:
    if not binary or not binary.strip():
        raise ToolInputError("command must be a non-empty binary name")
    if "/" in binary or "\\" in binary:
        raise ToolInputError(f"command must be a bare binary name, not a path: {binary!r}")
    if binary not in ALLOWED_BINARIES:
        raise DisallowedCommandError(
            f"binary {binary!r} is not on the Coder Agent allow-list. "
            f"Allowed: {', '.join(sorted(ALLOWED_BINARIES))}"
        )


async def run_command(
    deps: CoderDeps,
    binary: str,
    args: list[str] | None = None,
    *,
    timeout_s: int = 60,
) -> CommandResult:
    """Public async entry point — used both by the agent tool and by
    `validators.py` (which orchestrates several commands in parallel).
    """
    _validate_binary(binary)
    cleaned_args = list(args or [])
    for a in cleaned_args:
        if not isinstance(a, str):
            raise ToolInputError(f"command args must all be strings, got {type(a).__name__}")

    deps.bind(tool="run_command", binary=binary, args=cleaned_args, timeout_s=timeout_s).info(
        "coder.run_command"
    )

    if deps.sandbox is not None and deps.sandbox_handle is not None:
        return await _run_in_sandbox(
            deps.sandbox, deps.sandbox_handle, binary, cleaned_args, timeout_s=timeout_s
        )
    return await _run_subprocess(
        binary, cleaned_args, cwd=deps.workspace_root, timeout_s=timeout_s
    )


def _split_command(cmd: str) -> tuple[str, list[str]]:
    """Split a single command string into (binary, args) using shell
    quoting rules. We accept both `"pytest tests/test_foo.py"` and the
    pre-split form via `args=[...]`.
    """
    if not cmd.strip():
        raise ToolInputError("command must not be empty")
    parts = shlex.split(cmd)
    if not parts:
        raise ToolInputError("command parse produced no tokens")
    return parts[0], parts[1:]


def register(agent: Agent[CoderDeps, str]) -> None:
    """Attach `run_command` to `agent`."""

    @agent.tool
    async def run_command_tool(
        ctx: RunContext[CoderDeps],
        command: str,
        args: list[str] | None = None,
        timeout_s: int = 60,
    ) -> CommandResult:
        """Run a whitelisted developer command inside the sandbox.

        Only the following binaries are permitted: alembic, pytest, ruff,
        mypy, uv, python, npm, npx, pnpm, tsc, eslint, vitest. Anything
        else raises.

        You may pass the command either as a single string (`"pytest
        -xvs tests/test_foo.py"`) or pre-split as `command="pytest"` +
        `args=["-xvs", "tests/test_foo.py"]`. The latter form is
        preferred when arguments contain spaces or shell metacharacters.
        """
        if args is None:
            binary, split_args = _split_command(command)
        else:
            binary, split_args = command, list(args)
        return await run_command(ctx.deps, binary, split_args, timeout_s=timeout_s)
