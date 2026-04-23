"""Sandbox value types.

Kept in their own module so the Coder Agent and the preview route can
import them without dragging in the full `manager.py` (which pulls in
docker / subprocess code).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID


class SandboxError(RuntimeError):
    """Any sandbox lifecycle failure — boot, archive, resume, exec, git."""


class SandboxStatus(str, Enum):
    """Lifecycle states a sandbox moves through.

    `created` → `booting` → `running` → `archived` → `running` (on resume)
    `booting` → `failed` if `docker compose up` errors out.
    """

    CREATED = "created"
    BOOTING = "booting"
    RUNNING = "running"
    ARCHIVED = "archived"
    FAILED = "failed"
    DESTROYED = "destroyed"


@dataclass(frozen=True)
class SandboxHandle:
    """A reference to a sandbox.

    Callers get one from `manager.create()` / `manager.get()`. Handles are
    safe to cache — they don't go stale across archive/resume cycles.
    """

    id: str  # short stable id, e.g. "sbx-7f3a9b2c"
    project_id: UUID
    tenant_id: str
    workspace_path: Path


@dataclass
class SandboxInfo:
    """Live view of a sandbox's state — what `manager.info(handle)` returns."""

    handle: SandboxHandle
    status: SandboxStatus
    compose_project: str
    backend_port: int | None
    frontend_port: int | None
    preview_url: str | None
    created_at: datetime
    last_active_at: datetime | None
    archived_at: datetime | None
    last_error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.handle.id,
            "project_id": str(self.handle.project_id),
            "tenant_id": self.handle.tenant_id,
            "workspace_path": str(self.handle.workspace_path),
            "status": self.status.value,
            "compose_project": self.compose_project,
            "backend_port": self.backend_port,
            "frontend_port": self.frontend_port,
            "preview_url": self.preview_url,
            "created_at": self.created_at.isoformat(),
            "last_active_at": self.last_active_at.isoformat() if self.last_active_at else None,
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
            "last_error": self.last_error,
            "extra": self.extra,
        }
