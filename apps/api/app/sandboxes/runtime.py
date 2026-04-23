"""Container runtime abstraction.

`LocalSandboxManager` talks to Docker Compose through `ContainerRuntime`.
Tests substitute `FakeContainerRuntime` so we can exercise full manager
lifecycle without actually booting containers.

Roadmap §6 lists Daytona Cloud as the prod runtime, so we keep the
interface tight — everything a sandbox needs is {up, down, ps, exec,
logs}. Daytona's SDK maps 1:1 onto these verbs.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class ExecResult:
    """Outcome of a subprocess / container-exec call."""

    returncode: int
    stdout: str
    stderr: str
    # Command string for debug logging — not load-bearing; fine to leave "".
    cmd: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def raise_if_error(self) -> None:
        if not self.ok:
            from app.sandboxes.types import SandboxError

            raise SandboxError(
                f"Command failed ({self.returncode}): {self.cmd}\n"
                f"stderr: {self.stderr.strip()[:2000]}"
            )


class ContainerRuntime(Protocol):
    """Minimal surface for booting + driving a docker-compose stack."""

    async def up(
        self,
        compose_file: Path,
        project_name: str,
        *,
        env: dict[str, str] | None = None,
        wait: bool = True,
        timeout_s: int = 300,
    ) -> ExecResult: ...

    async def down(
        self,
        compose_file: Path,
        project_name: str,
        *,
        volumes: bool = False,
        timeout_s: int = 60,
    ) -> ExecResult: ...

    async def ps(
        self,
        compose_file: Path,
        project_name: str,
    ) -> ExecResult: ...

    async def exec(
        self,
        compose_file: Path,
        project_name: str,
        service: str,
        cmd: list[str],
        *,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        timeout_s: int = 120,
    ) -> ExecResult: ...

    async def logs(
        self,
        compose_file: Path,
        project_name: str,
        service: str | None = None,
        *,
        tail: int = 200,
    ) -> ExecResult: ...


class DockerComposeRuntime:
    """Async subprocess wrapper around `docker compose` v2.

    We use `docker compose` (plugin subcommand), not the deprecated
    `docker-compose` script. The binary must be on PATH — callers should
    call `.ensure_available()` at boot to surface a clear error if it
    isn't.
    """

    def __init__(self, binary: str = "docker") -> None:
        self._binary = binary

    async def ensure_available(self) -> None:
        """Fail fast if `docker compose version` doesn't work.

        Called from app startup so we surface a coherent 503 instead of
        the sandbox boot failing halfway through.
        """
        result = await self._run(["compose", "version"])
        if not result.ok:
            from app.sandboxes.types import SandboxError

            raise SandboxError(
                f"`{self._binary} compose` is not available. "
                f"Install Docker Desktop or the compose plugin. "
                f"stderr: {result.stderr.strip()[:500]}"
            )

    async def _run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout_s: int | None = None,
        stdin_data: bytes | None = None,
    ) -> ExecResult:
        full_env = {**os.environ, **(env or {})}
        proc = await asyncio.create_subprocess_exec(
            self._binary,
            *args,
            cwd=str(cwd) if cwd else None,
            env=full_env,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin_data),
                timeout=timeout_s,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ExecResult(
                returncode=124,
                stdout="",
                stderr=f"timed out after {timeout_s}s",
                cmd=f"{self._binary} {' '.join(args)}",
            )
        return ExecResult(
            returncode=proc.returncode if proc.returncode is not None else 1,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
            cmd=f"{self._binary} {' '.join(args)}",
        )

    def _compose_args(self, compose_file: Path, project_name: str) -> list[str]:
        return ["compose", "-p", project_name, "-f", str(compose_file)]

    async def up(
        self,
        compose_file: Path,
        project_name: str,
        *,
        env: dict[str, str] | None = None,
        wait: bool = True,
        timeout_s: int = 300,
    ) -> ExecResult:
        args = [*self._compose_args(compose_file, project_name), "up", "-d"]
        if wait:
            args.append("--wait")
            args.extend(["--wait-timeout", str(timeout_s)])
        return await self._run(args, cwd=compose_file.parent, env=env, timeout_s=timeout_s + 30)

    async def down(
        self,
        compose_file: Path,
        project_name: str,
        *,
        volumes: bool = False,
        timeout_s: int = 60,
    ) -> ExecResult:
        args = [*self._compose_args(compose_file, project_name), "down"]
        if volumes:
            args.append("-v")
        # `--timeout` here is the per-container SIGTERM→SIGKILL grace period.
        args.extend(["--timeout", "10"])
        return await self._run(args, cwd=compose_file.parent, timeout_s=timeout_s)

    async def ps(self, compose_file: Path, project_name: str) -> ExecResult:
        return await self._run(
            [*self._compose_args(compose_file, project_name), "ps", "--format", "json"],
            cwd=compose_file.parent,
            timeout_s=20,
        )

    async def exec(
        self,
        compose_file: Path,
        project_name: str,
        service: str,
        cmd: list[str],
        *,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        timeout_s: int = 120,
    ) -> ExecResult:
        # `-T` disables TTY — we want clean bytes piped back. `--no-deps`
        # not used: we want exec to fail fast if the service isn't up.
        args = [*self._compose_args(compose_file, project_name), "exec", "-T"]
        if workdir:
            args.extend(["-w", workdir])
        for k, v in (env or {}).items():
            args.extend(["-e", f"{k}={v}"])
        args.append(service)
        args.extend(cmd)
        return await self._run(args, cwd=compose_file.parent, timeout_s=timeout_s)

    async def logs(
        self,
        compose_file: Path,
        project_name: str,
        service: str | None = None,
        *,
        tail: int = 200,
    ) -> ExecResult:
        args = [
            *self._compose_args(compose_file, project_name),
            "logs",
            f"--tail={tail}",
            "--no-color",
        ]
        if service:
            args.append(service)
        return await self._run(args, cwd=compose_file.parent, timeout_s=30)


@dataclass
class _FakeCall:
    """One recorded call on `FakeContainerRuntime`."""

    action: str  # "up" | "down" | "ps" | "exec" | "logs"
    project_name: str
    compose_file: Path
    extra: dict[str, object] = field(default_factory=dict)


class FakeContainerRuntime:
    """In-memory double for tests.

    Records every call and returns a scripted `ExecResult`. Tests can
    pre-seed per-action responses via `.enqueue(action, result)`.
    """

    def __init__(self) -> None:
        self.calls: list[_FakeCall] = []
        self._scripted: dict[str, list[ExecResult]] = {}
        self.default_ok = ExecResult(returncode=0, stdout="", stderr="")

    def enqueue(self, action: str, result: ExecResult) -> None:
        self._scripted.setdefault(action, []).append(result)

    def _take(self, action: str) -> ExecResult:
        queue = self._scripted.get(action) or []
        if queue:
            return queue.pop(0)
        return self.default_ok

    async def ensure_available(self) -> None:
        return None

    async def up(
        self,
        compose_file: Path,
        project_name: str,
        *,
        env: dict[str, str] | None = None,
        wait: bool = True,
        timeout_s: int = 300,
    ) -> ExecResult:
        self.calls.append(
            _FakeCall(
                "up",
                project_name,
                compose_file,
                {"env": env or {}, "wait": wait, "timeout_s": timeout_s},
            )
        )
        return self._take("up")

    async def down(
        self,
        compose_file: Path,
        project_name: str,
        *,
        volumes: bool = False,
        timeout_s: int = 60,
    ) -> ExecResult:
        self.calls.append(_FakeCall("down", project_name, compose_file, {"volumes": volumes}))
        return self._take("down")

    async def ps(self, compose_file: Path, project_name: str) -> ExecResult:
        self.calls.append(_FakeCall("ps", project_name, compose_file))
        return self._take("ps")

    async def exec(
        self,
        compose_file: Path,
        project_name: str,
        service: str,
        cmd: list[str],
        *,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        timeout_s: int = 120,
    ) -> ExecResult:
        self.calls.append(
            _FakeCall(
                "exec",
                project_name,
                compose_file,
                {"service": service, "cmd": cmd, "workdir": workdir},
            )
        )
        return self._take("exec")

    async def logs(
        self,
        compose_file: Path,
        project_name: str,
        service: str | None = None,
        *,
        tail: int = 200,
    ) -> ExecResult:
        self.calls.append(
            _FakeCall("logs", project_name, compose_file, {"service": service, "tail": tail})
        )
        return self._take("logs")
