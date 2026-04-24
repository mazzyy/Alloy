"""Locate on-disk workspaces for a Project.

The IDE's file-tree + editor endpoints need to answer a simple question:
*given a `project_id`, where on disk is the code?* Sandboxes are the
canonical source — every sandbox state file carries `project_id` — so we
walk `ALLOY_WORKSPACES_ROOT/<tenant>/<sandbox_id>/.alloy/sandbox.json`
and pick the most recently-active match.

A project may have *multiple* sandbox directories over its lifetime (if a
previous one was destroyed and recreated). We pick the most recent by
`last_active_at` (falling back to `created_at`) so the Files API always
shows the sandbox currently backing the preview.

This is deliberately stateless — no in-memory index — so it keeps working
across API restarts and replicas without any warm-up step. Listing costs
O(sandboxes-in-tenant), capped in practice by the per-tenant sandbox
quota. We can add a Postgres `project.current_sandbox_id` column in
Phase 2 if the walk gets expensive.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog

_log = structlog.get_logger(__name__)

_STATE_FILENAME = "sandbox.json"


_SLUG_SAFE = set("abcdefghijklmnopqrstuvwxyz0123456789-")


def _tenant_dir_name(tenant_id: str) -> str:
    """Mirror `LocalSandboxManager._tenant_dir_name` — must stay in sync.

    Kept local (not imported) to avoid a runtime dependency on the
    sandbox manager module from the HTTP layer; the slug rules are
    simple and stable.
    """
    cleaned = "".join(c if c in _SLUG_SAFE else "-" for c in tenant_id.lower()).strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned or "default"


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def resolve_project_workspace(
    *, workspaces_root: Path, tenant_id: str, project_id: UUID
) -> Path | None:
    """Find the live workspace directory for a project under this tenant.

    Returns the absolute workspace path (the git repo root) if any
    sandbox state file matches, else None.

    * Filters strictly by `tenant_id` — a state file found under the
      wrong tenant directory is ignored even if `project_id` matches,
      so a forged `project_id` in one tenant can't reach another.
    * If multiple sandboxes match (rare — usually from a
      destroy-then-recreate cycle), picks the one with the latest
      `last_active_at` (falling back to `created_at`).
    """
    root = Path(workspaces_root).expanduser().resolve()
    tenant_dir = root / _tenant_dir_name(tenant_id)
    if not tenant_dir.is_dir():
        return None

    project_key = str(project_id)
    best_path: Path | None = None
    best_ts: datetime | None = None

    for state_path in tenant_dir.glob("*/.alloy/" + _STATE_FILENAME):
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Corrupt / unreadable state file — skip, don't fail the request.
            _log.warning("workspace.state_unreadable", path=str(state_path))
            continue

        if data.get("tenant_id") != tenant_id:
            # Defence in depth: state file lives under this tenant's
            # directory but declares a different tenant. Skip.
            continue
        if data.get("project_id") != project_key:
            continue

        status = data.get("status")
        if status == "destroyed":
            continue

        candidate = Path(data.get("workspace_path") or state_path.parent.parent)
        ts = _parse_dt(data.get("last_active_at")) or _parse_dt(data.get("created_at"))

        if best_path is None or (ts is not None and (best_ts is None or ts > best_ts)):
            best_path = candidate
            best_ts = ts

    if best_path is None:
        return None

    resolved = best_path.expanduser().resolve()
    # Final sanity: must still live under the tenant dir. Guards against
    # a state file that recorded a path outside its own directory.
    try:
        resolved.relative_to(tenant_dir.resolve())
    except ValueError:
        _log.warning(
            "workspace.path_outside_tenant",
            resolved=str(resolved),
            tenant_dir=str(tenant_dir),
        )
        return None

    if not resolved.is_dir():
        return None
    return resolved
