"""Phase 1 — build_runs table.

Revision ID: 0002_phase1_build_runs
Revises: 0001_phase1_projects
Create Date: 2026-04-25

Adds the `build_runs` table: one row per execution of a BuildPlan through
the LangGraph build loop. Powers `GET /build/runs/{id}` (live progress
re-attach) and the post-mortem audit trail.

Hand-authored — Phase 1 doesn't yet have a long-lived staging Postgres for
`alembic revision --autogenerate` to introspect. Keep in sync with
`app.models.project.BuildRun`.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0002_phase1_build_runs"
down_revision = "0001_phase1_projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "build_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("plan_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", sa.String(length=255), nullable=False),
        sa.Column("sandbox_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="running"),
        sa.Column("tasks_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tasks_run", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outcome_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("pending_review", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["plan_version_id"], ["build_plan_versions.id"]),
    )
    op.create_index("ix_build_runs_project_id", "build_runs", ["project_id"])
    op.create_index("ix_build_runs_tenant_id", "build_runs", ["tenant_id"])
    op.create_index(
        "ix_build_runs_project_started", "build_runs", ["project_id", "started_at"]
    )
    op.create_index(
        "ix_build_runs_tenant_status", "build_runs", ["tenant_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_build_runs_tenant_status", table_name="build_runs")
    op.drop_index("ix_build_runs_project_started", table_name="build_runs")
    op.drop_index("ix_build_runs_tenant_id", table_name="build_runs")
    op.drop_index("ix_build_runs_project_id", table_name="build_runs")
    op.drop_table("build_runs")
