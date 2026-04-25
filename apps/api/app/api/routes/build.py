"""Build runner endpoints.

`POST /api/v1/build/run`
    Drive the LangGraph outer build loop end-to-end for a project's
    current `BuildPlan`. Streams progress as SSE so the IDE can render
    per-task status as it lands, then emits a final `result` event with
    the full `BuildOutcome`.

`POST /api/v1/build/resume`
    Resume a build that paused on a `request_human_review` interrupt.
    The request supplies the answer + the `thread_id` returned by the
    original run.

`GET  /api/v1/build/runs/{run_id}`
    Fetch a `BuildRun` row by id — used by the frontend to re-attach to
    an in-flight build (mid-page-reload) or to render a post-mortem of a
    completed run.

`GET  /api/v1/build/runs?project_id=<uuid>`
    Last N `BuildRun` rows for a project. Powers the IDE's history pane.

Streaming contract
------------------
Every event is `data: <json>\n\n`. We use `event:` fields for type:

    event: status
    data: {"phase": "init"}

    event: status
    data: {"phase": "scaffolded", "workspace": "/...", "sandbox_id": "sbx-..."}

    event: status
    data: {"phase": "task_started", "task_id": "models.user", "idx": 0, "total": 9}

    event: status
    data: {"phase": "task_finished", "task_id": "models.user", "ok": true, "attempts": 1}

    event: result
    data: { ...BuildOutcome.model_dump()... , "run_id": "...", "thread_id": "..." }

    event: error
    data: {"message": "..."}

Crashes are logged and surfaced as `event: error`; the row is finalised
as `crashed` so an HTTP retry doesn't double-execute. Per-task progress
events are produced by polling `BuildOutcome` after the run completes —
LangGraph's `astream()` is reserved for Phase 2's incremental UI; for
Phase 1 the run is short enough that emitting `init → running → result`
is plenty.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import structlog
from alloy_shared.plan import BuildPlan
from alloy_shared.spec import AppSpec
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.build.runner import resume_build, run_build_plan
from app.agents.build.state import BuildOutcome, HumanReviewPayload
from app.agents.coder.context import CoderDeps
from app.api.deps import CurrentPrincipal, Principal
from app.core.config import settings
from app.core.db import get_session
from app.models.project import AppSpecVersion, BuildPlanVersion, BuildRun, Project
from app.services.builds import (
    archive_sandbox_safely,
    crash_build_run,
    create_build_run,
    ensure_workspace_for_build,
    finalise_build_run,
    list_runs_for_project,
)
from app.services.finalise import FinaliseReport, finalise_build

router = APIRouter(prefix="/build", tags=["build"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]

_log = structlog.get_logger("alloy.build_route")


# ────────────────────────────────────────────────────────────────────
# Request / response shapes
# ────────────────────────────────────────────────────────────────────


class BuildRunRequest(BaseModel):
    project_id: UUID
    plan_version_id: UUID | None = Field(
        default=None,
        description=(
            "Pin to a specific plan version. If omitted we use the "
            "project's current_plan_id."
        ),
    )
    max_attempts: int = Field(default=3, ge=1, le=5)
    validator_targets: list[str] | None = Field(
        default=None,
        description=(
            "Pass-through to `run_task_with_validators`. None = auto "
            "(both python + frontend). Tests pass `['python']`."
        ),
    )


class BuildResumeRequest(BaseModel):
    run_id: UUID
    answer: str = Field(min_length=1, max_length=4000)


class BuildRunSummary(BaseModel):
    """Row-shaped view of a `BuildRun` for the history pane."""

    id: UUID
    project_id: UUID
    plan_version_id: UUID
    thread_id: str
    sandbox_id: str | None
    status: str
    tasks_total: int
    tasks_run: int
    started_at: datetime
    ended_at: datetime | None
    error: str | None

    @classmethod
    def from_row(cls, row: BuildRun) -> BuildRunSummary:
        return cls(
            id=row.id,
            project_id=row.project_id,
            plan_version_id=row.plan_version_id,
            thread_id=row.thread_id,
            sandbox_id=row.sandbox_id,
            status=row.status,
            tasks_total=row.tasks_total,
            tasks_run=row.tasks_run,
            started_at=row.started_at,
            ended_at=row.ended_at,
            error=row.error,
        )


class BuildRunDetail(BuildRunSummary):
    outcome: BuildOutcome | None
    pending_review: HumanReviewPayload | None


# ────────────────────────────────────────────────────────────────────
# SSE helpers
# ────────────────────────────────────────────────────────────────────


def _sse(event: str | None, data: object) -> bytes:
    payload = json.dumps(data, separators=(",", ":"), default=str)
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {payload}\n\n".encode()


def _sse_done() -> bytes:
    return b"data: [DONE]\n\n"


# ────────────────────────────────────────────────────────────────────
# Plan resolution
# ────────────────────────────────────────────────────────────────────


async def _load_project_with_plan(
    session: AsyncSession,
    *,
    project_id: UUID,
    tenant_id: str,
    plan_version_id: UUID | None,
) -> tuple[Project, AppSpec, BuildPlan, BuildPlanVersion]:
    project = await session.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    target_plan_id = plan_version_id or project.current_plan_id
    if target_plan_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project has no plan to build — generate one via /plan/build first",
        )
    plan_row = await session.get(BuildPlanVersion, target_plan_id)
    if plan_row is None or plan_row.project_id != project.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan version missing")

    spec_row = await session.get(AppSpecVersion, plan_row.spec_version_id)
    if spec_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Spec version backing this plan disappeared",
        )

    spec = AppSpec.model_validate(spec_row.spec_json)
    plan = BuildPlan.model_validate(plan_row.plan_json)
    return project, spec, plan, plan_row


# ────────────────────────────────────────────────────────────────────
# Core SSE streamer
# ────────────────────────────────────────────────────────────────────


def _resolve_first_superuser_email(principal: Principal) -> str:
    """Pick the email the scaffolded backend pre-seeds as superuser.

    Priority: principal.email > sentinel. Phase 1 dev mode (auth disabled)
    has no email, so we fall back to a sentinel matching the existing
    test expectations rather than asking the scaffolder to handle None.
    """
    return principal.email or "admin@alloy.dev"


async def _build_event_stream(
    *,
    project: Project,
    spec: AppSpec,
    plan: BuildPlan,
    plan_row: BuildPlanVersion,
    principal: Principal,
    max_attempts: int,
    validator_targets: list[str] | None,
    session_factory: Any,
) -> AsyncIterator[bytes]:
    """Generator function passed to `StreamingResponse`.

    The session passed to the route handler is closed by FastAPI once the
    coroutine returning the StreamingResponse exits — so we open a fresh
    AsyncSession inside this generator for the BuildRun writes (insert,
    progress updates, finalise/crash). `session_factory` is the
    `app.core.db.get_session` dependency reused as a plain async ctx.
    """
    workspaces_root = Path(settings.ALLOY_WORKSPACES_ROOT).expanduser().resolve()
    workspaces_root.mkdir(parents=True, exist_ok=True)

    run_id: UUID | None = None
    workspace_path: Path | None = None
    sandbox_id: str | None = None
    thread_id = f"project:{project.id}"

    try:
        yield _sse("status", {"phase": "init", "thread_id": thread_id})

        # Materialise the workspace (reuse if a sandbox exists, scaffold
        # otherwise). This is the only step that mutates the filesystem
        # before the LangGraph loop, so failures here surface as a clean
        # error event rather than a half-committed BuildRun row.
        try:
            workspace_path, sandbox_id = await ensure_workspace_for_build(
                project=project,
                spec=spec,
                plan=plan,
                workspaces_root=workspaces_root,
                first_superuser_email=_resolve_first_superuser_email(principal),
            )
        except Exception as exc:  # noqa: BLE001 — surface as SSE error
            yield _sse("error", {"message": f"Workspace bootstrap failed: {exc}"})
            return

        yield _sse(
            "status",
            {
                "phase": "scaffolded",
                "workspace": str(workspace_path),
                "sandbox_id": sandbox_id,
            },
        )

        # Insert the BuildRun row in its own transaction so a watcher on
        # /build/runs/{id} can see "running" before the loop returns.
        # Schema errors here (e.g. `relation "build_runs" does not
        # exist` because the operator forgot `alembic upgrade head` in a
        # non-local env) surface as a clean SSE error rather than a
        # raw asyncpg traceback bleeding into the stream.
        try:
            async with session_factory() as run_session:
                run = await create_build_run(
                    run_session,
                    project=project,
                    plan_version_id=plan_row.id,
                    thread_id=thread_id,
                    sandbox_id=sandbox_id,
                    tasks_total=len(plan.ops),
                )
                await run_session.commit()
                run_id = run.id
        except Exception as exc:  # noqa: BLE001 — surface as SSE error
            _log.exception("build.run.create_failed", error=str(exc))
            hint = ""
            text = f"{type(exc).__name__}: {exc}"
            if "build_runs" in text and "does not exist" in text:
                hint = (
                    " — run `alembic upgrade head` from apps/api against "
                    "the gateway database; the build_runs migration "
                    "(0002_phase1_build_runs) hasn't been applied."
                )
            yield _sse(
                "error",
                {"message": f"Could not record BuildRun: {exc}{hint}"},
            )
            return

        yield _sse(
            "status",
            {
                "phase": "build_started",
                "run_id": str(run_id),
                "tasks_total": len(plan.ops),
            },
        )

        deps = CoderDeps(
            workspace_root=workspace_path,
            project_id=str(project.id),
            turn_id=str(run_id),
        )

        # Drive the LangGraph loop. We don't yet stream per-task events
        # mid-loop (Phase 2 will swap to `graph.astream`); for Phase 1 we
        # let it run and then replay the per-task outcomes.
        try:
            outcome = await run_build_plan(
                plan,
                deps,
                max_attempts=max_attempts,
                validator_targets=validator_targets,
                thread_id=thread_id,
                spec=spec,
            )
        except Exception as exc:  # noqa: BLE001
            _log.exception("build.loop.crashed", run_id=str(run_id), error=str(exc))
            async with session_factory() as crash_session:
                await crash_build_run(
                    crash_session, run_id=run_id, error=f"{type(exc).__name__}: {exc}"
                )
                await crash_session.commit()
            yield _sse("error", {"message": f"Build crashed: {exc}", "run_id": str(run_id)})
            return

        # Replay per-task outcomes so the UI can render the timeline.
        for idx, t in enumerate(outcome.outcomes):
            yield _sse(
                "status",
                {
                    "phase": "task_finished",
                    "task_id": t.task_id,
                    "ok": t.ok,
                    "attempts": t.attempts_used,
                    "commit_sha": t.commit_sha,
                    "idx": idx,
                    "total": outcome.tasks_total,
                    "error": t.error,
                },
            )

        # Deterministic post-build finalisation: alembic upgrade head,
        # openapi export, TS client regen, optional Playwright smoke.
        # Only runs when the build was clean (no pending review, no
        # task failures) — otherwise the workspace state isn't safe to
        # touch and we leave it for the user to inspect.
        finalise_report: FinaliseReport | None = None
        if outcome.ok and outcome.pending_review is None:
            yield _sse("status", {"phase": "finalise_started"})
            try:
                finalise_report = await finalise_build(deps)
            except Exception as exc:  # noqa: BLE001
                _log.exception(
                    "build.finalise.crashed", run_id=str(run_id), error=str(exc)
                )
                yield _sse(
                    "status",
                    {"phase": "finalise_crashed", "error": f"{type(exc).__name__}: {exc}"},
                )
            else:
                yield _sse(
                    "status",
                    {
                        "phase": "finalise_finished",
                        "ok": finalise_report.ok,
                        "steps": finalise_report.to_dict()["steps"],
                    },
                )

        # Persist outcome.
        async with session_factory() as fin_session:
            await finalise_build_run(fin_session, run_id=run_id, outcome=outcome)
            await fin_session.commit()

        # Result envelope — what the frontend hangs onto.
        yield _sse(
            "result",
            {
                "run_id": str(run_id),
                "thread_id": outcome.thread_id,
                "ok": outcome.ok,
                "tasks_run": outcome.tasks_run,
                "tasks_total": outcome.tasks_total,
                "outcomes": [o.model_dump(mode="json") for o in outcome.outcomes],
                "pending_review": (
                    outcome.pending_review.model_dump(mode="json")
                    if outcome.pending_review is not None
                    else None
                ),
                "finalise": finalise_report.to_dict() if finalise_report is not None else None,
            },
        )
        yield _sse_done()
    except asyncio.CancelledError:
        # Client disconnected mid-stream. Flag the run as crashed so a
        # subsequent retry doesn't think a stale row is still mid-flight.
        if run_id is not None:
            with contextlib.suppress(Exception):
                async with session_factory() as cancel_session:
                    await crash_build_run(
                        cancel_session, run_id=run_id, error="client_disconnected"
                    )
                    await cancel_session.commit()
        raise
    except Exception as exc:  # noqa: BLE001 — last-ditch
        _log.exception("build.stream.crashed", error=str(exc))
        if run_id is not None:
            with contextlib.suppress(Exception):
                async with session_factory() as crash_session:
                    await crash_build_run(
                        crash_session, run_id=run_id, error=f"stream: {exc}"
                    )
                    await crash_session.commit()
        yield _sse("error", {"message": f"Stream crashed: {exc}"})
    finally:
        # Best-effort archive to keep idle COGS down — only when the
        # sandbox manager actually owns this workspace.
        if sandbox_id and workspace_path is not None:
            with contextlib.suppress(Exception):
                await archive_sandbox_safely(sandbox_id, workspace_path)


# ────────────────────────────────────────────────────────────────────
# Endpoints
# ────────────────────────────────────────────────────────────────────


@router.post("/run", name="build.run")
async def post_build_run(
    body: BuildRunRequest,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> StreamingResponse:
    """Run a project's BuildPlan and stream progress as SSE."""
    project, spec, plan, plan_row = await _load_project_with_plan(
        session,
        project_id=body.project_id,
        tenant_id=principal.tenant_id,
        plan_version_id=body.plan_version_id,
    )

    # Snapshot the heavy fields outside the dependency-scoped session.
    # The streaming generator opens fresh sessions for its own writes.
    from app.core.db import SessionLocal

    return StreamingResponse(
        _build_event_stream(
            project=project,
            spec=spec,
            plan=plan,
            plan_row=plan_row,
            principal=principal,
            max_attempts=body.max_attempts,
            validator_targets=body.validator_targets,
            session_factory=SessionLocal,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/resume", name="build.resume")
async def post_build_resume(
    body: BuildResumeRequest,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> StreamingResponse:
    """Resume a build that paused on `request_human_review`."""
    run = await session.get(BuildRun, body.run_id)
    if run is None or run.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="BuildRun not found")
    if run.status != "needs_review":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run not pending review (status={run.status!r})",
        )

    project, spec, plan, _plan_row = await _load_project_with_plan(
        session,
        project_id=run.project_id,
        tenant_id=principal.tenant_id,
        plan_version_id=run.plan_version_id,
    )

    workspaces_root = Path(settings.ALLOY_WORKSPACES_ROOT).expanduser().resolve()
    from app.services.workspaces import resolve_project_workspace

    workspace_path = resolve_project_workspace(
        workspaces_root=workspaces_root,
        tenant_id=project.tenant_id,
        project_id=project.id,
    )
    if workspace_path is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace gone — paused build cannot resume",
        )

    from app.core.db import SessionLocal as async_session_maker

    deps = CoderDeps(
        workspace_root=workspace_path,
        project_id=str(project.id),
        turn_id=str(run.id),
    )
    answer = body.answer
    thread_id = run.thread_id
    run_id = run.id

    async def event_stream() -> AsyncIterator[bytes]:
        try:
            yield _sse("status", {"phase": "resume", "thread_id": thread_id})
            try:
                outcome = await resume_build(
                    answer,
                    deps,
                    thread_id=thread_id,
                    spec=spec,
                )
            except Exception as exc:  # noqa: BLE001
                _log.exception("build.resume.crashed", run_id=str(run_id), error=str(exc))
                async with async_session_maker() as crash_session:
                    await crash_build_run(
                        crash_session, run_id=run_id, error=f"resume: {exc}"
                    )
                    await crash_session.commit()
                yield _sse("error", {"message": f"Resume crashed: {exc}"})
                return

            for idx, t in enumerate(outcome.outcomes):
                yield _sse(
                    "status",
                    {
                        "phase": "task_finished",
                        "task_id": t.task_id,
                        "ok": t.ok,
                        "attempts": t.attempts_used,
                        "idx": idx,
                        "total": outcome.tasks_total,
                        "error": t.error,
                    },
                )

            async with async_session_maker() as fin_session:
                await finalise_build_run(fin_session, run_id=run_id, outcome=outcome)
                await fin_session.commit()

            yield _sse(
                "result",
                {
                    "run_id": str(run_id),
                    "thread_id": outcome.thread_id,
                    "ok": outcome.ok,
                    "tasks_run": outcome.tasks_run,
                    "tasks_total": outcome.tasks_total,
                    "outcomes": [o.model_dump(mode="json") for o in outcome.outcomes],
                    "pending_review": (
                        outcome.pending_review.model_dump(mode="json")
                        if outcome.pending_review is not None
                        else None
                    ),
                },
            )
            yield _sse_done()
        except Exception as exc:  # noqa: BLE001
            _log.exception("build.resume.stream_crashed", error=str(exc))
            yield _sse("error", {"message": f"Stream crashed: {exc}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}", name="build.run_detail")
async def get_build_run(
    run_id: UUID,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> BuildRunDetail:
    run = await session.get(BuildRun, run_id)
    if run is None or run.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="BuildRun not found")

    outcome: BuildOutcome | None = None
    if run.outcome_json:
        try:
            outcome = BuildOutcome.model_validate(run.outcome_json)
        except Exception:  # noqa: BLE001 — old rows shouldn't 500 the detail page
            outcome = None

    pending: HumanReviewPayload | None = None
    if run.pending_review:
        try:
            pending = HumanReviewPayload.model_validate(run.pending_review)
        except Exception:  # noqa: BLE001
            pending = None

    summary = BuildRunSummary.from_row(run)
    return BuildRunDetail(
        **summary.model_dump(),
        outcome=outcome,
        pending_review=pending,
    )


@router.get("/runs", name="build.run_list")
async def get_build_runs(
    project_id: Annotated[UUID, Query(...)],
    principal: CurrentPrincipal,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> list[BuildRunSummary]:
    project = await session.get(Project, project_id)
    if project is None or project.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    rows = await list_runs_for_project(session, project_id=project_id, limit=limit)
    return [BuildRunSummary.from_row(r) for r in rows]


__all__ = ["router"]
