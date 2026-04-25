"""Project / spec / plan persistence helpers.

Thin CRUD on top of the SQLModel tables. The routes call these; the agents
know nothing about the DB. Keeping persistence out of the agents is a trust
play — agents can be swapped or re-run without touching transaction logic.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any
from uuid import UUID, uuid4

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


async def _next_available_slug(
    session: AsyncSession, *, tenant_id: str, base: str
) -> str:
    """Return `base` if free in this tenant, otherwise `base-2`, `base-3`, ...

    The unique constraint is `(tenant_id, slug)`, so collisions are
    tenant-scoped. We probe in-Python rather than catching the IntegrityError
    because the DB error poisons the surrounding transaction with asyncpg
    and would force a rollback in the middle of `get_or_create_project`'s
    flow.

    Cap at 1000 attempts as a safety valve — if a tenant somehow has 1000
    "frontend-portfolio" projects, fall back to the UUID short suffix.

    Caveat: probe-then-insert is not atomic. Two concurrent requests can
    each see slug `X` as free and race to insert. Phase 1 doesn't gate
    on this — the failure mode was sequential retry from the same user.
    Phase 2 (multi-replica gateway) will need a SAVEPOINT-and-retry
    around the insert, or a `(tenant_id, slug)` advisory lock.
    """
    base = (base or "project")[:120]  # leave room for "-NNNN"
    stmt = select(Project.slug).where(
        col(Project.tenant_id) == tenant_id,
        col(Project.slug).startswith(base),
    )
    taken = {row for row in (await session.execute(stmt)).scalars().all()}
    if base not in taken:
        return base
    for n in range(2, 1000):
        candidate = f"{base}-{n}"
        if candidate not in taken:
            return candidate
    # Pathological tenant — fall back to a UUID4 short prefix.
    return f"{base}-{uuid4().hex[:8]}"


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

    Slug collisions: two different prompts can slugify to the same value
    ("Build a frontend portfolio" vs "Build a lightweight portfolio
    website based on frontend" both → "frontend-portfolio"). The
    `(tenant_id, slug)` unique index would reject the second insert with
    an asyncpg `UniqueViolationError`. We resolve by suffixing `-2`,
    `-3`, ... before insert. See `_next_available_slug`.
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
    base_slug = _slugify(display_name)[:128]
    slug = await _next_available_slug(session, tenant_id=tenant_id, base=base_slug)
    project = Project(
        tenant_id=tenant_id,
        slug=slug,
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
