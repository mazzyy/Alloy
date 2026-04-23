"""Sandbox orchestrator — creates, boots, archives, and resumes per-project workspaces.

A sandbox is:
* a directory on disk (`<workspaces_root>/<tenant>/<short_id>/`) containing
  a scaffolded project + a `compose.alloy.yml` we rendered + a git repo
* a set of docker-compose containers running under a project-unique
  compose project name
* two published host ports (frontend + backend)
* a persisted state file (`.alloy/sandbox.json`) so we can reload on
  API restart

The lifecycle verbs are intentionally small:

* `create()`      — scaffold the workspace, allocate ports, write state
* `boot()`        — `docker compose up -d --wait`; transitions to running
* `archive()`     — `docker compose down` (containers gone, disk intact)
* `resume()`      — re-allocate ports if needed, bring the stack back up
* `destroy()`     — `down -v` + `rm -rf` workspace
* `info()`        — read current state from disk + optional live ps check
* `exec()`        — run a command inside a named service (`backend`,
                    `frontend`, `db`) — used by the validator loop and
                    Coder Agent's `run_command` tool
* `list()`        — enumerate sandboxes this manager knows about

Design notes we care about:

* **All state on disk.** The API server is stateless; sandbox state lives
  in `.alloy/sandbox.json` + the filesystem itself. Restarting the API
  does not orphan sandboxes.
* **Fail closed.** If scaffolding or boot errors, we mark `FAILED` and
  leave the workspace untouched for inspection. Destroy only via the
  explicit `destroy()` call.
* **Tenant isolation.** Workspaces nest under `<workspaces_root>/<tenant>`.
  Later, when we swap this for Daytona, tenancy maps to Daytona org IDs.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import structlog
from alloy_shared.spec import AppSpec

from app.sandboxes.compose import ComposeRenderParams, preview_url_for, render_alloy_compose
from app.sandboxes.git_ops import commit_all, ensure_repo
from app.sandboxes.ports import PortAllocator, ProbingPortAllocator
from app.sandboxes.runtime import ContainerRuntime, DockerComposeRuntime
from app.sandboxes.types import SandboxError, SandboxHandle, SandboxInfo, SandboxStatus
from app.scaffold import (
    BlockCatalogue,
    ScaffoldError,
    scaffold_project,
)
from app.scaffold.blocks import Block

_log = structlog.get_logger(__name__)

_STATE_FILENAME = "sandbox.json"
_COMPOSE_FILENAME = "compose.alloy.yml"


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _short_id() -> str:
    return f"sbx-{uuid.uuid4().hex[:8]}"


_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _tenant_dir_name(tenant_id: str) -> str:
    cleaned = _SLUG_RE.sub("-", tenant_id.lower()).strip("-")
    return cleaned or "default"


class SandboxManager(Protocol):
    """Public interface. `LocalSandboxManager` is the only implementation
    today; Daytona and Fly.io Sprites slot in behind the same shape."""

    async def create(
        self,
        *,
        project_id: UUID,
        tenant_id: str,
        spec: AppSpec,
        blocks: list[Block],
        catalogue: BlockCatalogue,
        base_template_dir: Path,
        first_superuser_email: str,
    ) -> SandboxInfo: ...

    async def boot(self, handle: SandboxHandle) -> SandboxInfo: ...
    async def archive(self, handle: SandboxHandle) -> SandboxInfo: ...
    async def resume(self, handle: SandboxHandle) -> SandboxInfo: ...
    async def destroy(self, handle: SandboxHandle) -> None: ...
    async def info(self, handle: SandboxHandle) -> SandboxInfo: ...
    async def exec(
        self,
        handle: SandboxHandle,
        service: str,
        cmd: list[str],
        *,
        timeout_s: int = 120,
    ) -> tuple[int, str, str]: ...

    async def list(self, tenant_id: str | None = None) -> list[SandboxInfo]: ...


class LocalSandboxManager:
    """Docker-Compose-on-localhost implementation of `SandboxManager`.

    Thread-safety note: all lifecycle calls serialise on a per-sandbox
    asyncio lock stored in `self._locks`. This prevents interleaved
    `boot()` + `archive()` calls from scrambling state for the same
    sandbox. Multi-sandbox parallelism is fine — each has its own lock.
    """

    def __init__(
        self,
        *,
        workspaces_root: Path,
        port_allocator: PortAllocator | None = None,
        runtime: ContainerRuntime | None = None,
        preview_host: str = "localhost",
    ) -> None:
        self._root = workspaces_root.expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._ports = port_allocator or ProbingPortAllocator()
        self._runtime = runtime or DockerComposeRuntime()
        self._preview_host = preview_host
        self._locks: dict[str, asyncio.Lock] = {}

        # Rehydrate port claims from existing state files so a restart
        # doesn't double-allocate.
        self._prime_port_allocator()

    # ── State persistence ─────────────────────────────────────────

    def _prime_port_allocator(self) -> None:
        claimed: set[int] = set()
        for state_path in self._root.glob("*/*/.alloy/" + _STATE_FILENAME):
            try:
                data = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for key in ("backend_port", "frontend_port"):
                port = data.get(key)
                if isinstance(port, int):
                    claimed.add(port)
        # Only `ProbingPortAllocator` knows how to `.prime()`; others
        # (Redis, Fixed) don't need it.
        if hasattr(self._ports, "prime"):
            self._ports.prime(claimed)  # type: ignore[attr-defined]

    def _lock_for(self, sandbox_id: str) -> asyncio.Lock:
        lock = self._locks.get(sandbox_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[sandbox_id] = lock
        return lock

    def _workspace_path(self, tenant_id: str, sandbox_id: str) -> Path:
        return self._root / _tenant_dir_name(tenant_id) / sandbox_id

    def _state_path(self, workspace: Path) -> Path:
        return workspace / ".alloy" / _STATE_FILENAME

    def _compose_path(self, workspace: Path) -> Path:
        return workspace / _COMPOSE_FILENAME

    def _write_state(self, workspace: Path, info: SandboxInfo) -> None:
        path = self._state_path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(info.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _read_state(self, workspace: Path) -> dict[str, Any]:
        path = self._state_path(workspace)
        if not path.is_file():
            raise SandboxError(f"No sandbox state at {path}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SandboxError(f"Corrupt sandbox state at {path}: {exc}") from exc

    def _state_to_info(self, data: dict[str, Any]) -> SandboxInfo:
        handle = SandboxHandle(
            id=data["id"],
            project_id=UUID(data["project_id"]),
            tenant_id=data["tenant_id"],
            workspace_path=Path(data["workspace_path"]),
        )
        return SandboxInfo(
            handle=handle,
            status=SandboxStatus(data["status"]),
            compose_project=data["compose_project"],
            backend_port=data.get("backend_port"),
            frontend_port=data.get("frontend_port"),
            preview_url=data.get("preview_url"),
            created_at=datetime.fromisoformat(data["created_at"]),
            last_active_at=(
                datetime.fromisoformat(data["last_active_at"])
                if data.get("last_active_at")
                else None
            ),
            archived_at=(
                datetime.fromisoformat(data["archived_at"]) if data.get("archived_at") else None
            ),
            last_error=data.get("last_error"),
            extra=data.get("extra") or {},
        )

    # ── Lifecycle ─────────────────────────────────────────────────

    async def create(
        self,
        *,
        project_id: UUID,
        tenant_id: str,
        spec: AppSpec,
        blocks: list[Block],
        catalogue: BlockCatalogue,
        base_template_dir: Path,
        first_superuser_email: str,
    ) -> SandboxInfo:
        """Scaffold a workspace, allocate ports, render compose, write state.

        Does *not* boot — caller follows up with `.boot(handle)` when
        the user (or the LangGraph loop) is ready. This keeps create
        fast and lets the Coder Agent make a few initial edits before
        the first docker build.
        """
        sandbox_id = _short_id()
        workspace = self._workspace_path(tenant_id, sandbox_id)
        if workspace.exists():
            # Vanishingly unlikely given `uuid4().hex[:8]`, but the
            # crash here is cheaper than a silent mis-render.
            raise SandboxError(f"Workspace collision at {workspace}")

        # Scaffold first — if this fails, no port held.
        try:
            report = scaffold_project(
                spec,
                blocks=blocks,
                catalogue=catalogue,
                base_template_dir=base_template_dir,
                target_dir=workspace,
                first_superuser_email=first_superuser_email,
                # Scaffold-time git init; we add the sandbox-specific
                # compose file right after, then commit once.
                skip_git=False,
            )
        except ScaffoldError:
            # Leave partial dir for debug inspection — caller sees the
            # original exception.
            raise

        # Allocate two ports before rendering compose so compose points
        # at the right ones.
        backend_port = await self._ports.allocate()
        try:
            frontend_port = await self._ports.allocate(reserved={backend_port})
        except Exception:
            await self._ports.free(backend_port)
            raise

        compose_project = f"alloy-{sandbox_id}"
        env_vars = self._load_env_example(workspace)
        params = ComposeRenderParams(
            compose_project=compose_project,
            backend_port=backend_port,
            frontend_port=frontend_port,
            postgres_user=env_vars.get("POSTGRES_USER", "alloy"),
            postgres_password=env_vars.get("POSTGRES_PASSWORD", secrets.token_urlsafe(16)),
            postgres_db=env_vars.get("POSTGRES_DB", "alloy"),
            secret_key=env_vars.get("SECRET_KEY", secrets.token_urlsafe(32)),
            backend_env={
                k: v
                for k, v in env_vars.items()
                if k
                not in {
                    "POSTGRES_SERVER",
                    "POSTGRES_PORT",
                    "POSTGRES_USER",
                    "POSTGRES_PASSWORD",
                    "POSTGRES_DB",
                    "SECRET_KEY",
                }
            },
        )
        self._compose_path(workspace).write_text(render_alloy_compose(params), encoding="utf-8")
        self._append_gitignore(workspace)

        # Commit the baseline: scaffold + our compose file + .gitignore.
        await ensure_repo(workspace)
        try:
            await commit_all(
                workspace,
                message=f"feat: scaffold {spec.name} ({', '.join(b.name for b in blocks) or 'no blocks'})",
                allow_empty=False,
            )
        except SandboxError as exc:
            # Not fatal — the workspace is usable without a commit, but
            # log so we notice regressions in CI where git must be there.
            _log.warning(
                "sandbox.create.commit_failed",
                sandbox_id=sandbox_id,
                error=str(exc),
            )

        now = _utcnow()
        handle = SandboxHandle(
            id=sandbox_id,
            project_id=project_id,
            tenant_id=tenant_id,
            workspace_path=workspace,
        )
        info = SandboxInfo(
            handle=handle,
            status=SandboxStatus.CREATED,
            compose_project=compose_project,
            backend_port=backend_port,
            frontend_port=frontend_port,
            preview_url=preview_url_for(frontend_port, self._preview_host),
            created_at=now,
            last_active_at=None,
            archived_at=None,
            extra={
                "scaffold_report": report.to_dict(),
                "blocks": [b.name for b in blocks],
            },
        )
        self._write_state(workspace, info)
        _log.info(
            "sandbox.created",
            sandbox_id=sandbox_id,
            tenant_id=tenant_id,
            project_id=str(project_id),
            workspace=str(workspace),
            backend_port=backend_port,
            frontend_port=frontend_port,
        )
        return info

    async def boot(self, handle: SandboxHandle) -> SandboxInfo:
        async with self._lock_for(handle.id):
            info = self._load_info(handle)
            if info.status == SandboxStatus.RUNNING:
                return info
            if info.status == SandboxStatus.DESTROYED:
                raise SandboxError(f"Sandbox {handle.id} has been destroyed")
            info.status = SandboxStatus.BOOTING
            info.last_error = None
            self._write_state(handle.workspace_path, info)

            # If we're resuming after a restart and a port got taken,
            # re-allocate. Quick check: is the port still free?
            if not info.backend_port or not info.frontend_port:
                raise SandboxError(
                    f"Sandbox {handle.id} has no port allocation — state file corrupted."
                )

            result = await self._runtime.up(
                self._compose_path(handle.workspace_path),
                info.compose_project,
            )
            if not result.ok:
                info.status = SandboxStatus.FAILED
                info.last_error = result.stderr.strip()[:2000] or "docker compose up failed"
                self._write_state(handle.workspace_path, info)
                _log.error(
                    "sandbox.boot.failed",
                    sandbox_id=handle.id,
                    stderr=info.last_error,
                )
                raise SandboxError(info.last_error)

            info.status = SandboxStatus.RUNNING
            info.last_active_at = _utcnow()
            info.archived_at = None
            self._write_state(handle.workspace_path, info)
            _log.info("sandbox.booted", sandbox_id=handle.id)
            return info

    async def archive(self, handle: SandboxHandle) -> SandboxInfo:
        async with self._lock_for(handle.id):
            info = self._load_info(handle)
            if info.status in (SandboxStatus.ARCHIVED, SandboxStatus.CREATED):
                return info
            result = await self._runtime.down(
                self._compose_path(handle.workspace_path),
                info.compose_project,
            )
            # We still flip to archived on non-zero return — `down` often
            # exits 1 when containers don't exist, which is effectively
            # already-archived.
            info.status = SandboxStatus.ARCHIVED
            info.archived_at = _utcnow()
            if not result.ok:
                info.last_error = result.stderr.strip()[:1000]
            else:
                info.last_error = None
            self._write_state(handle.workspace_path, info)
            _log.info("sandbox.archived", sandbox_id=handle.id, ok=result.ok)
            return info

    async def resume(self, handle: SandboxHandle) -> SandboxInfo:
        """Same as boot(), but explicit about the state machine: only
        ARCHIVED or FAILED sandboxes can be resumed."""
        info = self._load_info(handle)
        if info.status not in (SandboxStatus.ARCHIVED, SandboxStatus.FAILED, SandboxStatus.CREATED):
            raise SandboxError(f"Cannot resume sandbox in status {info.status.value}")
        return await self.boot(handle)

    async def destroy(self, handle: SandboxHandle) -> None:
        async with self._lock_for(handle.id):
            info = self._load_info(handle)
            # Down with volumes — destroy means destroy.
            await self._runtime.down(
                self._compose_path(handle.workspace_path),
                info.compose_project,
                volumes=True,
            )
            if info.backend_port:
                await self._ports.free(info.backend_port)
            if info.frontend_port:
                await self._ports.free(info.frontend_port)
            # Remove the workspace last — past this point the state file
            # is gone, so the sandbox is unrecoverable.
            if handle.workspace_path.exists():
                shutil.rmtree(handle.workspace_path)
            self._locks.pop(handle.id, None)
            _log.info("sandbox.destroyed", sandbox_id=handle.id)

    async def info(self, handle: SandboxHandle) -> SandboxInfo:
        return self._load_info(handle)

    async def exec(
        self,
        handle: SandboxHandle,
        service: str,
        cmd: list[str],
        *,
        timeout_s: int = 120,
    ) -> tuple[int, str, str]:
        """Run `cmd` inside the named service's container.

        Returns `(returncode, stdout, stderr)` so callers (the validator
        loop, the Coder Agent's `run_command` tool) can make decisions
        without paying the `ExecResult` import cost.
        """
        info = self._load_info(handle)
        if info.status != SandboxStatus.RUNNING:
            raise SandboxError(
                f"Sandbox {handle.id} is {info.status.value}, must be running to exec"
            )
        # Refresh last_active_at so auto-archive doesn't reap us mid-command.
        info.last_active_at = _utcnow()
        self._write_state(handle.workspace_path, info)

        result = await self._runtime.exec(
            self._compose_path(handle.workspace_path),
            info.compose_project,
            service,
            cmd,
            timeout_s=timeout_s,
        )
        return result.returncode, result.stdout, result.stderr

    async def list(self, tenant_id: str | None = None) -> list[SandboxInfo]:
        """Enumerate sandboxes by walking the workspaces root.

        Optionally filtered by tenant. We read state files rather than
        an in-memory registry because the API server is stateless —
        multiple replicas will see the same disk content.
        """
        out: list[SandboxInfo] = []
        pattern = "*/*/.alloy/" + _STATE_FILENAME
        for state_path in sorted(self._root.glob(pattern)):
            try:
                data = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            info = self._state_to_info(data)
            if tenant_id is not None and info.handle.tenant_id != tenant_id:
                continue
            out.append(info)
        return out

    # ── Helpers ───────────────────────────────────────────────────

    def _load_info(self, handle: SandboxHandle) -> SandboxInfo:
        data = self._read_state(handle.workspace_path)
        if data["id"] != handle.id:
            raise SandboxError(f"Sandbox id mismatch: handle={handle.id} disk={data['id']}")
        return self._state_to_info(data)

    def _load_env_example(self, workspace: Path) -> dict[str, str]:
        """Parse `.env.example` into a dict.

        We read the scaffolded `.env.example` (which the blocks appended
        to) and forward every non-placeholder value to the backend
        container. Users can later override via `.env` (not yet wired).
        """
        env_file = workspace / ".env.example"
        if not env_file.is_file():
            return {}
        out: dict[str, str] = {}
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if not key:
                continue
            # Skip values that still look like placeholders — compose
            # would publish them as-is otherwise.
            if val.startswith("changeme") or val == "" or val.startswith("replace_me"):
                continue
            out[key] = val
        return out

    def _append_gitignore(self, workspace: Path) -> None:
        """Keep `compose.alloy.yml` + sandbox state out of git history.

        Both files encode machine state (host ports, compose project
        name) that changes per sandbox and shouldn't travel with a
        deploy.
        """
        gitignore = workspace / ".gitignore"
        marker = "# Alloy sandbox state"
        existing = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
        if marker in existing:
            return
        addition = f"\n{marker}\n{_COMPOSE_FILENAME}\n.alloy/sandbox.json\n"
        with gitignore.open("a", encoding="utf-8") as fh:
            fh.write(addition)
