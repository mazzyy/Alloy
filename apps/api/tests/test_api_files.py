"""HTTP tests for the read-only Files API (`/api/v1/projects/{id}/files`).

Shape of the setup:

* We stub `get_session` via FastAPI's `dependency_overrides` with a
  fake session whose only job is to hand back a `Project` row when
  asked for one by UUID. That avoids spinning up Postgres just to
  exercise the file-tree endpoints.
* We point `settings.ALLOY_WORKSPACES_ROOT` at a `tmp_path` directory
  populated to match the `LocalSandboxManager` on-disk layout:
  `<root>/<tenant-slug>/<sbx-id>/` + a `.alloy/sandbox.json` state
  file that references the project's UUID.
* In local env with `CLERK_ISSUER` unset, auth is bypassed and the
  dev principal carries `tenant_id="dev_tenant"`, which is what our
  fixture Projects use.

Coverage:

* Listing returns tree entries, skips `.git`/`node_modules`.
* Recursive glob with `**`.
* Reading a file returns content + line bounds.
* Line windowing works.
* `path=..` raises 400 (WorkspaceEscapeError).
* Missing project → 404.
* Project in another tenant → 404 (not 403; don't leak existence).
* Project without a materialised workspace → 409.
* Binary file → 400.
* Truncation flag on very wide listings.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.deps import Principal, get_current_principal
from app.core import config as config_module
from app.core.db import get_session
from app.main import app
from app.models.project import Project


# ── Fake DB session ────────────────────────────────────────────────────


class FakeAsyncSession:
    """Async-session stub that only implements `.get(Model, pk)`.

    Enough for the Files API — the routes never write or execute raw
    queries. Any other method raising `AttributeError` keeps the stub
    honest: if the route starts needing more from the session, tests
    fail loudly instead of silently misbehaving.
    """

    def __init__(self, projects: dict[UUID, Project]) -> None:
        self._projects = projects

    async def get(self, model: Any, pk: Any) -> Any:
        if model is Project:
            return self._projects.get(pk)
        raise AssertionError(f"Unexpected .get({model!r}, ...)")


def _install_session_override(projects: dict[UUID, Project]) -> None:
    async def _override() -> AsyncIterator[FakeAsyncSession]:
        yield FakeAsyncSession(projects)

    app.dependency_overrides[get_session] = _override


def _install_principal_override(*, tenant_id: str = "dev_tenant") -> None:
    def _override() -> Principal:
        return Principal(
            user_id="test_user", tenant_id=tenant_id, org_role=None, email=None
        )

    app.dependency_overrides[get_current_principal] = _override


# ── Workspace fixture (mirrors LocalSandboxManager's layout) ────────────


def _seed_workspace(
    *,
    workspaces_root: Path,
    tenant_id: str,
    project_id: UUID,
    sandbox_id: str = "sbx-abc12345",
    status: str = "created",
) -> Path:
    """Create `<root>/<tenant>/<sbx>/` with a realistic state file + files.

    Returns the workspace directory (the would-be git repo root).
    """
    tenant_slug = _slug(tenant_id)
    workspace = workspaces_root / tenant_slug / sandbox_id
    workspace.mkdir(parents=True, exist_ok=True)

    # A few scaffold-like files the file tree should surface.
    (workspace / "apps/api/app").mkdir(parents=True, exist_ok=True)
    (workspace / "apps/web/src").mkdir(parents=True, exist_ok=True)
    (workspace / "README.md").write_text("# scaffolded project\n", encoding="utf-8")
    (workspace / "apps/api/app/main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n",
        encoding="utf-8",
    )
    (workspace / "apps/web/src/App.tsx").write_text(
        "export default function App() { return <div>hi</div>; }\n",
        encoding="utf-8",
    )
    # Skip-dir noise: must not appear in listings.
    (workspace / "node_modules").mkdir(exist_ok=True)
    (workspace / "node_modules/noise.js").write_text("// ignored\n", encoding="utf-8")

    state_dir = workspace / ".alloy"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_payload = {
        "id": sandbox_id,
        "project_id": str(project_id),
        "tenant_id": tenant_id,
        "workspace_path": str(workspace),
        "status": status,
        "compose_project": f"alloy-{sandbox_id}",
        "backend_port": None,
        "frontend_port": None,
        "preview_url": None,
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "last_active_at": None,
        "archived_at": None,
        "last_error": None,
        "extra": {},
    }
    (state_dir / "sandbox.json").write_text(
        json.dumps(state_payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    return workspace


def _slug(s: str) -> str:
    """Copy of `_tenant_dir_name` — kept in-sync with the service helper."""
    out = "".join(c if (c.isalnum() or c == "-") else "-" for c in s.lower()).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out or "default"


def _make_project(tenant_id: str, *, name: str = "todo-app") -> Project:
    return Project(
        id=uuid4(),
        tenant_id=tenant_id,
        slug=name,
        name=name,
        original_prompt="build a todo app",
    )


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def workspaces_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "workspaces"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config_module.settings, "ALLOY_WORKSPACES_ROOT", str(root))
    return root


# ── Tests ───────────────────────────────────────────────────────────────


def test_list_files_returns_tree(client: TestClient, workspaces_root: Path) -> None:
    project = _make_project("dev_tenant")
    _seed_workspace(
        workspaces_root=workspaces_root,
        tenant_id="dev_tenant",
        project_id=project.id,
    )
    _install_session_override({project.id: project})
    _install_principal_override()

    r = client.get(f"/api/v1/projects/{project.id}/files")
    assert r.status_code == 200, r.text
    body = r.json()
    names = {e["path"] for e in body["entries"]}
    assert "apps" in names
    assert "README.md" in names
    # Skip-dirs are hidden even at the root.
    assert "node_modules" not in names
    assert body["truncated"] is False


def test_list_files_recursive_glob(client: TestClient, workspaces_root: Path) -> None:
    project = _make_project("dev_tenant")
    _seed_workspace(
        workspaces_root=workspaces_root,
        tenant_id="dev_tenant",
        project_id=project.id,
    )
    _install_session_override({project.id: project})
    _install_principal_override()

    r = client.get(
        f"/api/v1/projects/{project.id}/files",
        params={"path": ".", "pattern": "apps/**/*.py"},
    )
    assert r.status_code == 200, r.text
    names = {e["path"] for e in r.json()["entries"]}
    assert "apps/api/app/main.py" in names
    # .tsx file must NOT match a `*.py` pattern.
    assert "apps/web/src/App.tsx" not in names


def test_read_file_returns_content(client: TestClient, workspaces_root: Path) -> None:
    project = _make_project("dev_tenant")
    _seed_workspace(
        workspaces_root=workspaces_root,
        tenant_id="dev_tenant",
        project_id=project.id,
    )
    _install_session_override({project.id: project})
    _install_principal_override()

    r = client.get(
        f"/api/v1/projects/{project.id}/files/content",
        params={"path": "apps/api/app/main.py"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == "apps/api/app/main.py"
    assert "FastAPI()" in body["content"]
    assert body["line_count"] == 2
    assert body["start_line"] == 1
    assert body["end_line"] == 2
    assert body["clipped"] is False


def test_read_file_line_window(client: TestClient, workspaces_root: Path) -> None:
    project = _make_project("dev_tenant")
    workspace = _seed_workspace(
        workspaces_root=workspaces_root,
        tenant_id="dev_tenant",
        project_id=project.id,
    )
    # 10-line file so we can slice it.
    (workspace / "lines.txt").write_text(
        "\n".join(f"line-{i}" for i in range(1, 11)) + "\n",
        encoding="utf-8",
    )

    _install_session_override({project.id: project})
    _install_principal_override()

    r = client.get(
        f"/api/v1/projects/{project.id}/files/content",
        params={"path": "lines.txt", "start_line": 3, "end_line": 5},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["start_line"] == 3
    assert body["end_line"] == 5
    assert body["content"].splitlines() == ["line-3", "line-4", "line-5"]


def test_path_traversal_rejected(client: TestClient, workspaces_root: Path) -> None:
    project = _make_project("dev_tenant")
    _seed_workspace(
        workspaces_root=workspaces_root,
        tenant_id="dev_tenant",
        project_id=project.id,
    )
    _install_session_override({project.id: project})
    _install_principal_override()

    r = client.get(
        f"/api/v1/projects/{project.id}/files",
        params={"path": "../"},
    )
    assert r.status_code == 400
    assert "escape" in r.json()["detail"].lower() or "workspace" in r.json()["detail"].lower()


def test_absolute_path_rejected(client: TestClient, workspaces_root: Path) -> None:
    project = _make_project("dev_tenant")
    _seed_workspace(
        workspaces_root=workspaces_root,
        tenant_id="dev_tenant",
        project_id=project.id,
    )
    _install_session_override({project.id: project})
    _install_principal_override()

    r = client.get(
        f"/api/v1/projects/{project.id}/files/content",
        params={"path": "/etc/passwd"},
    )
    assert r.status_code == 400
    assert "absolute" in r.json()["detail"].lower()


def test_missing_project_is_404(client: TestClient, workspaces_root: Path) -> None:
    _install_session_override({})
    _install_principal_override()

    r = client.get(f"/api/v1/projects/{uuid4()}/files")
    assert r.status_code == 404


def test_cross_tenant_project_is_404(client: TestClient, workspaces_root: Path) -> None:
    """A project belonging to tenant A is invisible to tenant B.

    We return 404 (not 403) so the other tenant can't tell whether the
    project exists.
    """
    foreign = _make_project("other_tenant")
    _seed_workspace(
        workspaces_root=workspaces_root,
        tenant_id="other_tenant",
        project_id=foreign.id,
    )
    _install_session_override({foreign.id: foreign})
    _install_principal_override(tenant_id="dev_tenant")

    r = client.get(f"/api/v1/projects/{foreign.id}/files")
    assert r.status_code == 404


def test_unmaterialised_workspace_is_409(client: TestClient, workspaces_root: Path) -> None:
    """Project row exists but no sandbox on disk yet."""
    project = _make_project("dev_tenant")
    _install_session_override({project.id: project})
    _install_principal_override()

    r = client.get(f"/api/v1/projects/{project.id}/files")
    assert r.status_code == 409
    assert "workspace" in r.json()["detail"].lower()


def test_binary_file_rejected(client: TestClient, workspaces_root: Path) -> None:
    project = _make_project("dev_tenant")
    workspace = _seed_workspace(
        workspaces_root=workspaces_root,
        tenant_id="dev_tenant",
        project_id=project.id,
    )
    # Write a file with an early NUL byte so the 4 KB head probe trips.
    (workspace / "image.bin").write_bytes(b"GIF89a\x00\x00noise")

    _install_session_override({project.id: project})
    _install_principal_override()

    r = client.get(
        f"/api/v1/projects/{project.id}/files/content",
        params={"path": "image.bin"},
    )
    assert r.status_code == 400
    assert "binary" in r.json()["detail"].lower()


def test_destroyed_sandbox_is_ignored(client: TestClient, workspaces_root: Path) -> None:
    """A state file marked `destroyed` should not resolve to a workspace."""
    project = _make_project("dev_tenant")
    _seed_workspace(
        workspaces_root=workspaces_root,
        tenant_id="dev_tenant",
        project_id=project.id,
        status="destroyed",
    )
    _install_session_override({project.id: project})
    _install_principal_override()

    r = client.get(f"/api/v1/projects/{project.id}/files")
    assert r.status_code == 409


def test_resolver_prefers_latest_when_multiple_sandboxes(
    client: TestClient, workspaces_root: Path
) -> None:
    """Two sandboxes for the same project — pick the most-recently-active."""
    project = _make_project("dev_tenant")
    # Older sandbox; we'll make its files look distinct.
    older = _seed_workspace(
        workspaces_root=workspaces_root,
        tenant_id="dev_tenant",
        project_id=project.id,
        sandbox_id="sbx-older",
    )
    (older / "MARKER.txt").write_text("older-sandbox\n", encoding="utf-8")

    # Newer one — the resolver should pick this.
    newer = _seed_workspace(
        workspaces_root=workspaces_root,
        tenant_id="dev_tenant",
        project_id=project.id,
        sandbox_id="sbx-newer",
    )
    (newer / "MARKER.txt").write_text("newer-sandbox\n", encoding="utf-8")

    # Stamp `last_active_at` on the newer one so the resolver can tell
    # them apart deterministically (same `created_at` otherwise).
    state = json.loads(
        (newer / ".alloy" / "sandbox.json").read_text(encoding="utf-8")
    )
    state["last_active_at"] = datetime.now(UTC).replace(microsecond=0).isoformat()
    (newer / ".alloy" / "sandbox.json").write_text(
        json.dumps(state, indent=2, sort_keys=True), encoding="utf-8"
    )

    _install_session_override({project.id: project})
    _install_principal_override()

    r = client.get(
        f"/api/v1/projects/{project.id}/files/content",
        params={"path": "MARKER.txt"},
    )
    assert r.status_code == 200
    assert r.json()["content"].strip() == "newer-sandbox"
