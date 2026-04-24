"""BuildRun persistence + workspace resolution helpers.

The build-run SSE endpoint composes three concerns:
1. Persist the `BuildRun` row so the UI can re-attach mid-flight.
2. Make sure a sandbox exists for this project (creating one via the
   deterministic scaffolder + block overlay if not).
3. Drive the LangGraph loop, persisting the final outcome.

This module owns (1) and (2). The route owns (3) so FastAPI's streaming
generator stays close to its SSE plumbing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import structlog
from alloy_shared.plan import BuildPlan
from alloy_shared.spec import AppSpec
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.build.state import BuildOutcome
from app.models.project import BuildRun, Project
from app.sandboxes.dependency import (
    get_base_template_dir,
    get_block_catalogue,
    get_sandbox_manager,
)
from app.sandboxes.types import (
    SandboxError,
    SandboxHandle,
    SandboxInfo,
    SandboxStatus,
)
from app.services.workspaces import resolve_project_workspace

_log = structlog.get_logger(__name__)


# ────────────────────────────────────────────────────────────────────
# BuildRun CRUD
# ────────────────────────────────────────────────────────────────────


def _utcnow_naive() -> datetime:
    """The `BuildRun` timestamp columns are tz-aware in DDL but `_utcnow`
    in the rest of `models.project` strips tzinfo to match SQLite-friendly
    behaviour. Match that convention so re-loaded rows compare equal."""
    return datetime.now(UTC).replace(tzinfo=None)


async def create_build_run(
    session: AsyncSession,
    *,
    project: Project,
    plan_version_id: UUID,
    thread_id: str,
    sandbox_id: str | None,
    tasks_total: int,
) -> BuildRun:
    """Insert a `running` row before the LangGraph loop starts.

    The endpoint commits this immediately so a UI client connecting via
    `GET /build/runs/{id}` sees the in-flight row even before any task
    finishes.
    """
    row = BuildRun(
        project_id=project.id,
        tenant_id=project.tenant_id,
        plan_version_id=plan_version_id,
        thread_id=thread_id,
        sandbox_id=sandbox_id,
        status="running",
        tasks_total=tasks_total,
        tasks_run=0,
        started_at=_utcnow_naive(),
    )
    session.add(row)
    await session.flush()
    return row


async def update_build_run_progress(
    session: AsyncSession, *, run_id: UUID, tasks_run: int
) -> None:
    """Update the running task counter mid-loop.

    Lightweight — no full row load. Used by the SSE handler after each task
    so reconnecting clients see real progress, not a stale 0/N until the run
    finishes.
    """
    run = await session.get(BuildRun, run_id)
    if run is None:
        return
    run.tasks_run = tasks_run
    await session.flush()


async def finalise_build_run(
    session: AsyncSession,
    *,
    run_id: UUID,
    outcome: BuildOutcome,
    error: str | None = None,
) -> BuildRun:
    """Record terminal outcome. `outcome.pending_review` decides status."""
    run = await session.get(BuildRun, run_id)
    if run is None:
        raise LookupError(f"BuildRun {run_id} disappeared mid-flight")

    if outcome.pending_review is not None:
        run.status = "needs_review"
        run.pending_review = outcome.pending_review.model_dump(mode="json")
    elif outcome.ok:
        run.status = "succeeded"
        run.pending_review = None
    else:
        run.status = "failed"
        run.pending_review = None

    run.tasks_run = outcome.tasks_run
    run.tasks_total = outcome.tasks_total
    run.outcome_json = outcome.model_dump(mode="json")
    run.error = error
    run.ended_at = _utcnow_naive()
    await session.flush()
    return run


async def crash_build_run(
    session: AsyncSession, *, run_id: UUID, error: str
) -> None:
    """Mark a run as crashed when the SSE generator itself errored.

    Distinct from `failed` (validators rejected an attempt) so we can grep
    for orchestration bugs separately from generation regressions.
    """
    run = await session.get(BuildRun, run_id)
    if run is None:
        return
    run.status = "crashed"
    run.error = error
    run.ended_at = _utcnow_naive()
    await session.flush()


async def list_runs_for_project(
    session: AsyncSession, *, project_id: UUID, limit: int = 25
) -> list[BuildRun]:
    """Most-recent runs first. Used by the IDE's history panel."""
    stmt = (
        select(BuildRun)
        .where(BuildRun.project_id == project_id)
        .order_by(BuildRun.started_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars())


# ────────────────────────────────────────────────────────────────────
# Workspace lifecycle (sandbox bootstrap)
# ────────────────────────────────────────────────────────────────────


async def ensure_workspace_for_build(
    *,
    project: Project,
    spec: AppSpec,
    plan: BuildPlan,
    workspaces_root: Path,
    first_superuser_email: str,
) -> tuple[Path, str | None]:
    """Return `(workspace_path, sandbox_id)` ready for the LangGraph loop.

    If a live sandbox already exists for this project we reuse it — the
    Coder Agent's edits should layer on prior turns, not start from a fresh
    scaffold each build. If none exists we run the deterministic scaffolder
    against the base template + the plan's resolved blocks and return the
    new workspace.

    `sandbox_id` may be None when reusing a non-sandbox workspace (e.g. a
    test harness that pre-seeded a directory). Callers tolerate this — the
    LangGraph loop only needs a writable git-initialised dir.
    """
    existing = resolve_project_workspace(
        workspaces_root=workspaces_root,
        tenant_id=project.tenant_id,
        project_id=project.id,
    )
    if existing is not None:
        sandbox_id = _sandbox_id_for(existing)
        _log.info(
            "build.workspace.reused",
            project_id=str(project.id),
            workspace=str(existing),
            sandbox_id=sandbox_id,
        )
        return existing, sandbox_id

    manager = get_sandbox_manager()
    catalogue = get_block_catalogue()
    base_template_dir = get_base_template_dir()
    blocks = catalogue.get_many(plan.blocks)
    catalogue.assert_no_conflicts([b.name for b in blocks])

    info: SandboxInfo = await manager.create(
        project_id=project.id,
        tenant_id=project.tenant_id,
        spec=spec,
        blocks=blocks,
        catalogue=catalogue,
        base_template_dir=base_template_dir,
        first_superuser_email=first_superuser_email,
    )
    _log.info(
        "build.workspace.created",
        project_id=str(project.id),
        sandbox_id=info.handle.id,
        workspace=str(info.handle.workspace_path),
        blocks=plan.blocks,
    )
    return info.handle.workspace_path, info.handle.id


async def archive_sandbox_safely(sandbox_id: str | None, workspace: Path | None) -> None:
    """Best-effort archive after a build ends.

    Failures here don't surface to the user — archive is an optimisation,
    not part of the build's success contract. Logging is enough.
    """
    if not sandbox_id or workspace is None:
        return
    manager = get_sandbox_manager()
    handle = SandboxHandle(
        id=sandbox_id,
        project_id=UUID(int=0),  # not used by archive — the state file carries the truth
        tenant_id="",  # ditto
        workspace_path=workspace,
    )
    try:
        info = await manager.info(handle)
    except SandboxError:
        return
    if info.status != SandboxStatus.RUNNING:
        return
    try:
        await manager.archive(info.handle)
    except SandboxError as exc:
        _log.warning("build.workspace.archive_failed", sandbox_id=sandbox_id, error=str(exc))


def _sandbox_id_for(workspace: Path) -> str | None:
    """Read `.alloy/sandbox.json` and return the sandbox id, if any."""
    state = workspace / ".alloy" / "sandbox.json"
    if not state.is_file():
        return None
    try:
        import json as _json

        return _json.loads(state.read_text(encoding="utf-8")).get("id")
    except (OSError, ValueError):
        return None


__all__ = [
    "archive_sandbox_safely",
    "crash_build_run",
    "create_build_run",
    "ensure_workspace_for_build",
    "finalise_build_run",
    "list_runs_for_project",
    "update_build_run_progress",
]
