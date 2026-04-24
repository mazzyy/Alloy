"""Project, AppSpecVersion, BuildPlanVersion — Phase 1 persistence.

Every Alloy-generated app is a **Project**. A Project accrues versioned
`AppSpecVersion` rows (one per time the user or the Spec Agent edits the
spec) and `BuildPlanVersion` rows (one per Planner run). We keep full
history because the product's roadblocks are "user regenerated and lost
work" and "what did the agent actually build" — answerable from row diffs.

Multi-tenancy: every table carries `tenant_id`. Phase 4 turns on
`FORCE ROW LEVEL SECURITY` with a `SET LOCAL app.current_tenant` hook;
until then the FastAPI dependency filters explicitly.

Design notes:

* `spec_json` / `plan_json` store the full Pydantic-serialized object as
  JSONB. The shared Pydantic models are the source of truth; the DB is a
  replay log. We do *not* normalize entities/routes/pages into their own
  tables — they're regenerated from the spec on every edit.
* `sha` columns let us dedup: if the user clicks "propose" twice on the
  same prompt we can surface the same spec rather than paying twice.
* IDs are UUID v4 stored as `uuid` (Postgres native) — we avoid int PKs
  because projects cross tenants and UUIDs stop enumeration attacks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Column, Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Project(SQLModel, table=True):
    __tablename__ = "projects"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: str = Field(index=True, nullable=False)

    # Stable slug scoped to tenant — path segment in /projects/<slug>.
    slug: str = Field(index=True, nullable=False)
    name: str = Field(nullable=False)

    # The user's original prompt that kicked off the project. Kept for
    # replay/debug and so later sessions can show "what you originally asked".
    original_prompt: str = Field(nullable=False)

    # Pointers to the currently-active spec/plan versions. The version rows
    # themselves are append-only; these FKs move forward when the user
    # accepts a new proposal.
    current_spec_id: UUID | None = Field(default=None, index=True)
    current_plan_id: UUID | None = Field(default=None, index=True)

    created_at: datetime = Field(default_factory=_utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=_utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_projects_tenant_slug"),
        Index("ix_projects_tenant_created", "tenant_id", "created_at"),
    )


class AppSpecVersion(SQLModel, table=True):
    __tablename__ = "app_spec_versions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    project_id: UUID = Field(foreign_key="projects.id", index=True, nullable=False)
    tenant_id: str = Field(index=True, nullable=False)

    # Monotonically increasing per project. 1 on first save.
    version: int = Field(nullable=False)

    # SHA-256 of the canonical JSON of the spec. Dedup key.
    sha: str = Field(index=True, nullable=False)

    # Full AppSpec (pydantic .model_dump()) serialized as JSONB.
    spec_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))

    # Origin of the spec: "agent" | "user_edit" | "import".
    origin: str = Field(default="agent", nullable=False)

    # Which Azure deployment produced it (auditable for model regressions).
    model_name: str | None = Field(default=None, nullable=True)

    created_at: datetime = Field(default_factory=_utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_spec_versions_project_version"),
    )


class BuildPlanVersion(SQLModel, table=True):
    __tablename__ = "build_plan_versions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    project_id: UUID = Field(foreign_key="projects.id", index=True, nullable=False)
    tenant_id: str = Field(index=True, nullable=False)

    # Pointer to the spec this plan was derived from.
    spec_version_id: UUID = Field(foreign_key="app_spec_versions.id", nullable=False)

    version: int = Field(nullable=False)
    sha: str = Field(index=True, nullable=False)

    # Full BuildPlan serialized.
    plan_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))

    model_name: str | None = Field(default=None, nullable=True)

    # Execution status. For Phase 1 first slice we stop at "proposed" — no
    # Coder Agent yet. Phase 1 wk5 extends: "running" / "completed" / "failed".
    status: str = Field(default="proposed", nullable=False)

    # Back-pointer for polymorphic JSON lookups in dev (e.g. via psql).
    extra: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))

    created_at: datetime = Field(default_factory=_utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_plan_versions_project_version"),
    )


class BuildRun(SQLModel, table=True):
    """One execution of a BuildPlan through the LangGraph build loop.

    A new BuildRun row is created the moment `POST /api/v1/build/run` enters
    its SSE handler. We persist progress incrementally (status + tasks_run +
    pending_review) so the UI can reattach to an in-flight run and so we have
    a permanent audit trail of what got executed.

    Status values:
        "running"        — the LangGraph loop is actively running.
        "succeeded"      — every task green, repo committed.
        "failed"         — at least one task exhausted retries.
        "needs_review"   — paused on a `request_human_review` interrupt.
        "crashed"        — the SSE generator itself crashed (rare).
    """

    __tablename__ = "build_runs"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    project_id: UUID = Field(foreign_key="projects.id", index=True, nullable=False)
    tenant_id: str = Field(index=True, nullable=False)
    plan_version_id: UUID = Field(foreign_key="build_plan_versions.id", nullable=False)

    # LangGraph thread_id — `project:<uuid>` by default. Required for
    # `resume_build()` after a needs_review pause.
    thread_id: str = Field(nullable=False)

    # Sandbox the build wrote into. Optional — adhoc builds without a
    # sandbox (tests, CLI runs) leave this null.
    sandbox_id: str | None = Field(default=None, nullable=True)

    status: str = Field(default="running", nullable=False)

    tasks_total: int = Field(default=0, nullable=False)
    tasks_run: int = Field(default=0, nullable=False)

    # Final outcome payload (mirror of `BuildOutcome.model_dump()`). Populated
    # when the run terminates so a re-fetch shows the same per-task summary
    # the SSE stream emitted live.
    outcome_json: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )

    # Pending human-review payload while status="needs_review". Cleared on
    # resume.
    pending_review: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )

    error: str | None = Field(default=None, nullable=True)

    started_at: datetime = Field(default_factory=_utcnow, nullable=False)
    ended_at: datetime | None = Field(default=None, nullable=True)

    __table_args__ = (
        Index("ix_build_runs_project_started", "project_id", "started_at"),
        Index("ix_build_runs_tenant_status", "tenant_id", "status"),
    )
