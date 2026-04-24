"""Project listing endpoint.

`GET /api/v1/projects?limit=50&offset=0` — returns the current tenant's
projects ordered by updated_at descending.

This is the minimal endpoint needed by the Dashboard to render the project
card grid. Phase 2 adds search, filtering, and pagination cursors.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.api.deps import CurrentPrincipal
from app.core.db import get_session
from app.models.project import Project

router = APIRouter(prefix="/projects", tags=["projects"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class ProjectSummary(BaseModel):
    id: str
    slug: str
    name: str
    status: str
    has_spec: bool
    has_plan: bool
    created_at: str
    updated_at: str


class ProjectListResponse(BaseModel):
    projects: list[ProjectSummary]
    total: int


@router.get("", name="projects.list")
async def list_projects(
    principal: CurrentPrincipal,
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ProjectListResponse:
    """List the current tenant's projects, newest first."""
    # Count total.
    count_stmt = (
        select(func.count())
        .select_from(Project)
        .where(col(Project.tenant_id) == principal.tenant_id)
    )
    total = (await session.execute(count_stmt)).scalar_one()

    # Fetch page.
    stmt = (
        select(Project)
        .where(col(Project.tenant_id) == principal.tenant_id)
        .order_by(col(Project.updated_at).desc())
        .offset(offset)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()

    projects = [
        ProjectSummary(
            id=str(p.id),
            slug=p.slug,
            name=p.name,
            status="active",
            has_spec=p.current_spec_id is not None,
            has_plan=p.current_plan_id is not None,
            created_at=p.created_at.isoformat() + "Z",
            updated_at=p.updated_at.isoformat() + "Z",
        )
        for p in rows
    ]

    return ProjectListResponse(projects=projects, total=total)
