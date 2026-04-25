"""SQLModel table registry.

Alembic's autogenerate walks `SQLModel.metadata` at import time — any model
we want in migrations must be imported here. Tests and the runtime both
import `app.models` as the single seam.
"""

from __future__ import annotations

from app.models.project import AppSpecVersion, BuildPlanVersion, BuildRun, Project

__all__ = ["Project", "AppSpecVersion", "BuildPlanVersion", "BuildRun"]
