"""Planner Agent endpoints.

`POST /api/v1/plan/build` — run the Planner Agent on a project's current
(or a caller-supplied) AppSpec and persist the resulting BuildPlan.

`GET  /api/v1/plan/{project_id}` — fetch the current plan for a project.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from alloy_shared.plan import BuildPlan
from alloy_shared.spec import AppSpec
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.models import AgentModelConfigError
from app.agents.planner_agent import (
    build_planner_agent,
    build_planner_user_prompt,
    resolve_blocks_for_spec,
)
from app.api.deps import CurrentPrincipal
from app.core.config import settings
from app.core.db import get_session
from app.models.project import AppSpecVersion, BuildPlanVersion, Project
from app.services.projects import save_plan_version

router = APIRouter(prefix="/plan", tags=["plan"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class PlanBuildRequest(BaseModel):
    project_id: UUID


class PlanEnvelope(BaseModel):
    project_id: UUID
    project_slug: str
    plan_version_id: UUID
    plan_version: int
    plan: BuildPlan


def _sse(event: str | None, data: object) -> bytes:
    payload = json.dumps(data, separators=(",", ":"), default=str)
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {payload}\n\n".encode()


@router.post("/build", name="plan.build")
async def build_plan(
    body: PlanBuildRequest, principal: CurrentPrincipal, session: SessionDep
) -> StreamingResponse:
    project = await session.get(Project, body.project_id)
    if project is None or project.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    if project.current_spec_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project has no spec to plan from",
        )
    spec_row = await session.get(AppSpecVersion, project.current_spec_id)
    if spec_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Spec version missing")

    spec = AppSpec.model_validate(spec_row.spec_json)
    project_id = project.id
    project_slug = project.slug
    tenant_id = project.tenant_id  # noqa: F841 — reserved for RLS hook in Phase 4
    spec_version_id = spec_row.id

    async def event_stream() -> AsyncIterator[bytes]:
        try:
            yield _sse("status", {"phase": "init"})

            blocks = resolve_blocks_for_spec(spec)
            yield _sse("status", {"phase": "blocks_resolved", "blocks": blocks})

            try:
                agent = build_planner_agent()
            except AgentModelConfigError as exc:
                yield _sse("error", {"message": str(exc)})
                return

            yield _sse("status", {"phase": "model_call"})
            try:
                result = await agent.run(build_planner_user_prompt(spec, blocks))
            except Exception as exc:  # noqa: BLE001
                yield _sse("error", {"message": f"Planner Agent failed: {exc}"})
                return

            plan: BuildPlan = result.output

            # Guarantee the planner didn't drift on blocks / slug — we pin
            # these deterministically on the server side.
            plan = plan.model_copy(update={"blocks": blocks, "spec_slug": spec.slug})

            yield _sse("status", {"phase": "persisting"})

            row = await save_plan_version(
                session,
                project=project,
                spec_version_id=spec_version_id,
                plan=plan,
                model_name=settings.AZURE_OPENAI_DEPLOYMENT or None,
            )
            await session.commit()

            envelope = PlanEnvelope(
                project_id=project_id,
                project_slug=project_slug,
                plan_version_id=row.id,
                plan_version=row.version,
                plan=plan,
            )
            yield _sse("result", envelope.model_dump(mode="json"))
            yield b"data: [DONE]\n\n"
        except Exception as exc:  # noqa: BLE001
            yield _sse("error", {"message": f"Plan stream crashed: {exc}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{project_id}", name="plan.get")
async def get_plan(
    project_id: UUID, principal: CurrentPrincipal, session: SessionDep
) -> PlanEnvelope:
    project = await session.get(Project, project_id)
    if project is None or project.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    if project.current_plan_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No plan for this project"
        )
    row = await session.get(BuildPlanVersion, project.current_plan_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan version missing")
    return PlanEnvelope(
        project_id=project.id,
        project_slug=project.slug,
        plan_version_id=row.id,
        plan_version=row.version,
        plan=BuildPlan.model_validate(row.plan_json),
    )
