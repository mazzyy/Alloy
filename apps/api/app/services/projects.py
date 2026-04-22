"""Project / spec / plan persistence helpers.

Thin CRUD on top of the SQLModel tables. The routes call these; the agents
know nothing about the DB. Keeping persistence out of the agents is a trust
play — agents can be swapped or re-run without touching transaction logic.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any
from uuid import UUID

from alloy_shared.plan import BuildPlan
from alloy_shared.spec import AppSpec
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.models.project import AppSpecVersion, BuildPlanVersion, Project


def _slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    s = re.sub(r"-+", "-", s)
    return s or "project"


def _canonical_json_sha(obj: Any) -> str:
    """Stable SHA-256 of a Pydantic model. We use `model_dump_json()` which
    is deterministic — Pydantic orders dict keys by field order, which is
    itself deterministic from the schema.
    """
    if hasattr(obj, "model_dump_json"):
        data = obj.model_dump_json()
    else:
        import json

        data = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


async def get_or_create_project(
    session: AsyncSession,
    *,
    tenant_id: str,
    prompt: str,
    name: str | None = None,
) -> Project:
    """Find a recent project in this tenant matching the prompt, else create one.

    We match on tenant + exact prompt to avoid creating a blizzard of empty
    project rows when the user retries. The UI will grow an explicit
    "New project" button in Phase 1 wk6 — until then this is the dedup
    policy.
    """
    stmt = (
        select(Project)
        .where(col(Project.tenant_id) == tenant_id, col(Project.original_prompt) == prompt)
        .order_by(col(Project.created_at).desc())
        .limit(1)
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing

    display_name = (name or prompt.split("\n", 1)[0] or "Untitled project").strip()[:200]
    project = Project(
        tenant_id=tenant_id,
        slug=_slugify(display_name)[:128],
        name=display_name,
        original_prompt=prompt,
    )
    session.add(project)
    await session.flush()
    return project


async def next_version(
    session: AsyncSession, *, project_id: UUID, table: type[AppSpecVersion | BuildPlanVersion]
) -> int:
    stmt = select(func.coalesce(func.max(col(table.version)), 0)).where(
        col(table.project_id) == project_id
    )
    current = (await session.execute(stmt)).scalar_one() or 0
    return int(current) + 1


async def save_spec_version(
    session: AsyncSession,
    *,
    project: Project,
    spec: AppSpec,
    origin: str = "agent",
    model_name: str | None = None,
) -> AppSpecVersion:
    version = await next_version(session, project_id=project.id, table=AppSpecVersion)
    row = AppSpecVersion(
        project_id=project.id,
        tenant_id=project.tenant_id,
        version=version,
        sha=_canonical_json_sha(spec),
        spec_json=spec.model_dump(mode="json"),
        origin=origin,
        model_name=model_name,
    )
    session.add(row)
    await session.flush()
    project.current_spec_id = row.id
    return row


async def save_plan_version(
    session: AsyncSession,
    *,
    project: Project,
    spec_version_id: UUID,
    plan: BuildPlan,
    model_name: str | None = None,
) -> BuildPlanVersion:
    version = await next_version(session, project_id=project.id, table=BuildPlanVersion)
    row = BuildPlanVersion(
        project_id=project.id,
        tenant_id=project.tenant_id,
        spec_version_id=spec_version_id,
        version=version,
        sha=_canonical_json_sha(plan),
        plan_json=plan.model_dump(mode="json"),
        model_name=model_name,
    )
    session.add(row)
    await session.flush()
    project.current_plan_id = row.id
    return row
