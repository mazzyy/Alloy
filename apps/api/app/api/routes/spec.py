"""Spec Agent endpoints.

`POST /api/v1/spec/propose` — run the Spec Agent on a user prompt and
return the proposed `AppSpec` (streaming progress + final object).

`POST /api/v1/spec/save` — persist a user-edited `AppSpec` as a new
`AppSpecVersion` under the project.

`GET  /api/v1/spec/{project_id}` — fetch the current spec for a project.

Streaming protocol (roadmap §2 — WSS tokens + tool events):
    event: status\\ndata: {"phase": "model_call"}\\n\\n
    event: status\\ndata: {"phase": "validating"}\\n\\n
    event: result\\ndata: {"project_id": "...", "spec_version_id": "...", "spec": {...}}\\n\\n
    data: [DONE]\\n\\n

We use discrete events (not raw tokens) because the Spec Agent emits tool
call arguments, not free-form text — mid-stream tokens would be JSON
fragments useless to the UI. Token-level streaming returns for the Coder
Agent in Phase 1 wk5.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from alloy_shared.spec import AppSpec
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.agents.models import AgentModelConfigError
from app.agents.spec_agent import build_spec_agent, build_user_prompt
from app.api.deps import CurrentPrincipal
from app.core.config import settings
from app.core.db import get_session
from app.models.project import AppSpecVersion, Project
from app.services.projects import (
    _canonical_json_sha,  # re-used for idempotent save
    get_or_create_project,
    save_spec_version,
)

router = APIRouter(prefix="/spec", tags=["spec"])


SessionDep = Annotated[AsyncSession, Depends(get_session)]


# ── Request / response models ────────────────────────────────────────────


class SpecProposeRequest(BaseModel):
    prompt: str = Field(..., min_length=4, max_length=8000)
    clarifying_answers: dict[str, str] = Field(default_factory=dict)
    project_name: str | None = Field(default=None, max_length=255)


class SpecSaveRequest(BaseModel):
    project_id: UUID
    spec: AppSpec


class SpecEnvelope(BaseModel):
    project_id: UUID
    project_slug: str
    spec_version_id: UUID
    spec_version: int
    spec: AppSpec


# ── SSE helpers ──────────────────────────────────────────────────────────


def _sse(event: str | None, data: object) -> bytes:
    payload = json.dumps(data, separators=(",", ":"), default=str)
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {payload}\n\n".encode()


# ── Routes ───────────────────────────────────────────────────────────────


@router.post("/propose", name="spec.propose")
async def propose_spec(
    body: SpecProposeRequest, principal: CurrentPrincipal, session: SessionDep
) -> StreamingResponse:
    """Run the Spec Agent on a prompt, persist the result, stream progress."""

    # We capture the values we need up front so the generator doesn't close
    # over the session after the surrounding function returns.
    prompt = body.prompt
    clarifying = body.clarifying_answers
    project_name = body.project_name
    tenant_id = principal.tenant_id

    async def event_stream() -> AsyncIterator[bytes]:
        try:
            yield _sse("status", {"phase": "init"})

            try:
                agent = build_spec_agent()
            except AgentModelConfigError as exc:
                yield _sse("error", {"message": str(exc)})
                return

            yield _sse("status", {"phase": "model_call"})

            user_msg = build_user_prompt(prompt, clarifying or None)
            try:
                result = await agent.run(user_msg)
            except Exception as exc:  # noqa: BLE001 — surface provider/validation errors
                yield _sse("error", {"message": f"Spec Agent failed: {exc}"})
                return

            spec: AppSpec = result.output

            yield _sse("status", {"phase": "persisting"})

            project = await get_or_create_project(
                session, tenant_id=tenant_id, prompt=prompt, name=project_name or spec.name
            )
            spec_version = await save_spec_version(
                session,
                project=project,
                spec=spec,
                origin="agent",
                model_name=settings.AZURE_OPENAI_DEPLOYMENT or None,
            )
            await session.commit()

            envelope = SpecEnvelope(
                project_id=project.id,
                project_slug=project.slug,
                spec_version_id=spec_version.id,
                spec_version=spec_version.version,
                spec=spec,
            )
            yield _sse("result", envelope.model_dump(mode="json"))
            yield b"data: [DONE]\n\n"
        except Exception as exc:  # noqa: BLE001 — last-ditch streaming error
            yield _sse("error", {"message": f"Spec stream crashed: {exc}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/save", name="spec.save")
async def save_spec(
    body: SpecSaveRequest, principal: CurrentPrincipal, session: SessionDep
) -> SpecEnvelope:
    """Persist a user-edited spec. Idempotent on (project_id, sha)."""
    project = await session.get(Project, body.project_id)
    if project is None or project.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    new_sha = _canonical_json_sha(body.spec)
    # Short-circuit: if the latest spec already has this SHA, return it.
    stmt = (
        select(AppSpecVersion)
        .where(
            col(AppSpecVersion.project_id) == project.id,
            col(AppSpecVersion.sha) == new_sha,
        )
        .order_by(col(AppSpecVersion.version).desc())
        .limit(1)
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        project.current_spec_id = existing.id
        await session.commit()
        return SpecEnvelope(
            project_id=project.id,
            project_slug=project.slug,
            spec_version_id=existing.id,
            spec_version=existing.version,
            spec=body.spec,
        )

    row = await save_spec_version(session, project=project, spec=body.spec, origin="user_edit")
    await session.commit()
    return SpecEnvelope(
        project_id=project.id,
        project_slug=project.slug,
        spec_version_id=row.id,
        spec_version=row.version,
        spec=body.spec,
    )


@router.get("/{project_id}", name="spec.get")
async def get_spec(
    project_id: UUID, principal: CurrentPrincipal, session: SessionDep
) -> SpecEnvelope:
    project = await session.get(Project, project_id)
    if project is None or project.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    if project.current_spec_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No spec for this project"
        )
    row = await session.get(AppSpecVersion, project.current_spec_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Spec version missing")
    return SpecEnvelope(
        project_id=project.id,
        project_slug=project.slug,
        spec_version_id=row.id,
        spec_version=row.version,
        spec=AppSpec.model_validate(row.spec_json),
    )
