"""Read-only Files API for the IDE.

The frontend needs two things to render the file tree and editor:

* `GET /api/v1/projects/{project_id}/files?path=...&pattern=...`
    — shallow or recursive directory listing (recursive only when
      `pattern` contains `**`).
* `GET /api/v1/projects/{project_id}/files/content?path=...`
    — contents of a single file, optionally windowed by line range.

Both endpoints are **read-only**. Edits go through the Coder Agent / the
apply-patch pipeline — there is no user-facing save endpoint in Phase 1.

Tenant isolation is enforced twice:

1. The `Project` row must carry the caller's `tenant_id`, else 404.
2. `resolve_project_workspace` only walks the caller's tenant directory
   on disk. A forged project_id for a sibling tenant can't leak paths.

Path safety comes for free — `_list_dir` / `_read_file` use
`resolve_inside()` which rejects absolute paths, parent traversal, and
symlink escapes. We translate `ToolInputError` → HTTP 400 and
`WorkspaceEscapeError` → HTTP 400 so the frontend can surface the
underlying message.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.coder.errors import ToolInputError, WorkspaceEscapeError
from app.agents.coder.results import FileList, FileRead
from app.agents.coder.tools.files import _list_dir, _read_file
from app.api.deps import CurrentPrincipal
from app.core.config import settings
from app.core.db import get_session
from app.models.project import Project
from app.services.workspaces import resolve_project_workspace

router = APIRouter(prefix="/projects", tags=["files"])


SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def _load_project_workspace(
    project_id: UUID, principal_tenant_id: str, session: AsyncSession
) -> tuple[Project, Path]:
    """Resolve the project + its on-disk workspace, 404-ing on any miss.

    We return a 404 (not 403) when the tenant doesn't match — leaking
    "project exists but not yours" would give cross-tenant enumeration
    attackers a probe oracle.
    """
    project = await session.get(Project, project_id)
    if project is None or project.tenant_id != principal_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )

    workspaces_root = Path(settings.ALLOY_WORKSPACES_ROOT).expanduser()
    workspace = resolve_project_workspace(
        workspaces_root=workspaces_root,
        tenant_id=project.tenant_id,
        project_id=project.id,
    )
    if workspace is None:
        # The project exists in Postgres but no sandbox has been
        # scaffolded yet (common for a brand-new project whose spec is
        # still being proposed). Tell the frontend clearly so it can
        # render an empty-state rather than a generic 404.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Project has no materialised workspace yet. Run the build first.",
        )
    return project, workspace


@router.get("/{project_id}/files", name="files.list")
async def list_files(
    project_id: UUID,
    principal: CurrentPrincipal,
    session: SessionDep,
    path: str = Query(default=".", description="Workspace-relative directory."),
    pattern: str | None = Query(
        default=None,
        description=(
            "Optional fnmatch glob. If it contains `**` the walk is recursive; "
            "otherwise we only match filenames in the immediate directory."
        ),
    ),
) -> FileList:
    """List files/directories under a workspace path.

    Returns at most 500 entries; callers should paginate by pattern
    (e.g. list a subdirectory) when the tree is large. `truncated=True`
    signals the cap was hit.
    """
    _, workspace = await _load_project_workspace(project_id, principal.tenant_id, session)
    try:
        return _list_dir(workspace, path, pattern)
    except (ToolInputError, WorkspaceEscapeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/{project_id}/files/content", name="files.read")
async def read_file(
    project_id: UUID,
    principal: CurrentPrincipal,
    session: SessionDep,
    path: str = Query(..., description="Workspace-relative file path."),
    start_line: int | None = Query(
        default=None, ge=1, description="1-indexed first line to return (inclusive)."
    ),
    end_line: int | None = Query(
        default=None, ge=1, description="1-indexed last line to return (inclusive)."
    ),
) -> FileRead:
    """Return the contents of a single file, optionally windowed.

    Returns at most 2 000 lines per call (`clipped=True` signals we hit
    the cap). Binary files (detected via a NUL-byte probe on the first
    4 KB) return 400 — the IDE opens them via a separate preview route.
    """
    _, workspace = await _load_project_workspace(project_id, principal.tenant_id, session)
    try:
        return _read_file(workspace, path, start_line, end_line)
    except (ToolInputError, WorkspaceEscapeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
