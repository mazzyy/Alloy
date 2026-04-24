"""Sandbox info endpoint.

`GET /api/v1/projects/{project_id}/sandbox` — returns the sandbox status
and preview URL for a project. Used by the frontend's preview panel to
toggle between Sandpack (lite) and iframe (full) preview modes.

If no sandbox exists, returns `{"status": "none", "preview_url": null}`.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import CurrentPrincipal
from app.core.config import settings
from app.services.workspaces import resolve_project_workspace

router = APIRouter(prefix="/projects", tags=["sandbox"])


class SandboxInfoResponse(BaseModel):
    status: str
    preview_url: str | None
    backend_port: int | None
    frontend_port: int | None
    sandbox_id: str | None
    workspace_path: str | None


@router.get("/{project_id}/sandbox", name="projects.sandbox_info")
async def get_sandbox_info(
    project_id: UUID,
    principal: CurrentPrincipal,
) -> SandboxInfoResponse:
    """Return sandbox status and preview URL for a project.

    Reads the sandbox state file from disk — no container runtime
    queries. If the sandbox is running, `preview_url` will point to
    the frontend port on the preview host.
    """
    import json
    from pathlib import Path

    workspaces_root = Path(settings.ALLOY_WORKSPACES_ROOT).expanduser().resolve()
    workspace = resolve_project_workspace(
        workspaces_root=workspaces_root,
        tenant_id=principal.tenant_id,
        project_id=project_id,
    )

    if workspace is None:
        return SandboxInfoResponse(
            status="none",
            preview_url=None,
            backend_port=None,
            frontend_port=None,
            sandbox_id=None,
            workspace_path=None,
        )

    # Read the sandbox state file.
    state_path = workspace / ".alloy" / "sandbox.json"
    if not state_path.is_file():
        return SandboxInfoResponse(
            status="none",
            preview_url=None,
            backend_port=None,
            frontend_port=None,
            sandbox_id=None,
            workspace_path=str(workspace),
        )

    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return SandboxInfoResponse(
            status="error",
            preview_url=None,
            backend_port=None,
            frontend_port=None,
            sandbox_id=None,
            workspace_path=str(workspace),
        )

    return SandboxInfoResponse(
        status=data.get("status", "unknown"),
        preview_url=data.get("preview_url"),
        backend_port=data.get("backend_port"),
        frontend_port=data.get("frontend_port"),
        sandbox_id=data.get("id"),
        workspace_path=str(workspace),
    )
