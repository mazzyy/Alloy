"""Sandbox manager end-to-end test.

Uses the scaffolder's synthetic minimal Copier template + the fake
container runtime so we can exercise every lifecycle transition in
milliseconds, without touching Docker.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from alloy_shared.spec import (
    AppSpec,
    AuthConfig,
    Entity,
    EntityField,
    Integration,
    Page,
    Route,
)

from app.sandboxes import (
    FakeContainerRuntime,
    FixedPortAllocator,
    LocalSandboxManager,
    SandboxStatus,
)
from app.sandboxes.runtime import ExecResult
from app.sandboxes.types import SandboxError
from app.scaffold import load_catalogue
from app.scaffold.blocks import Block, BlockCatalogue, BlockFile, BlockPatch

REPO_ROOT = Path(__file__).resolve().parents[3]
BLOCKS_DIR = REPO_ROOT / "blocks"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


# ── Fixtures (mirrors test_scaffold.py's minimal template) ─────────


def _sample_spec(slug: str = "task-tracker") -> AppSpec:
    return AppSpec(
        name="Task Tracker",
        slug=slug,
        description="Tracks tasks.",
        auth=AuthConfig(provider="clerk", allow_signup=True, require_email_verify=False),
        entities=[
            Entity(
                name="Task",
                plural="Tasks",
                auditable=False,
                fields=[
                    EntityField(
                        name="id",
                        type="uuid",
                        required=True,
                        unique=True,
                        indexed=True,
                    ),
                ],
            )
        ],
        routes=[
            Route(
                method="GET",
                path="/tasks",
                handler_name="list_tasks",
                permission="authenticated",
            )
        ],
        pages=[Page(name="Tasks", path="/tasks", data_deps=["list_tasks"])],
        integrations=[Integration(kind="clerk")],
        schema_version=1,
    )


def _minimal_template(tmp_path: Path) -> Path:
    src = tmp_path / "template"
    src.mkdir()
    (src / "copier.yml").write_text(
        "project_name:\n  type: str\n  default: My App\n"
        "stack_name:\n  type: str\n  default: my-app\n",
        encoding="utf-8",
    )
    (src / "VERSION").write_text("1.0.0-test", encoding="utf-8")
    (src / "README.md.jinja").write_text("# {{ project_name }}\n", encoding="utf-8")
    backend_dir = src / "backend" / "app" / "api"
    backend_dir.mkdir(parents=True)
    (backend_dir / "main.py").write_text(
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n# <<ALLOY_ROUTER_REGISTER>>\n",
        encoding="utf-8",
    )
    frontend_pkg = src / "frontend"
    frontend_pkg.mkdir()
    (frontend_pkg / "package.json").write_text(
        '{\n  "name": "scaffold-test",\n  "dependencies": {}\n}\n',
        encoding="utf-8",
    )
    # Real shipped frontend blocks (auth/clerk) patch into
    # `frontend/src/main.tsx` against `<<ALLOY_PROVIDER_WRAP>>`. Provide
    # a minimal stub so the catalogue-integration smoke test doesn't
    # fail with "patches missing file frontend/src/main.tsx" the moment
    # we ship a frontend-touching block.
    frontend_src = frontend_pkg / "src"
    frontend_src.mkdir()
    (frontend_src / "main.tsx").write_text(
        "import { StrictMode } from 'react';\n"
        "import { createRoot } from 'react-dom/client';\n"
        "createRoot(document.getElementById('root')!).render(\n"
        "  <StrictMode>\n"
        "    {/* <<ALLOY_PROVIDER_WRAP>> */}\n"
        "    <div>scaffold-test</div>\n"
        "  </StrictMode>\n"
        ");\n",
        encoding="utf-8",
    )
    (src / ".env.example").write_text(
        "POSTGRES_USER=alloy\nPOSTGRES_PASSWORD=alloy\nPOSTGRES_DB=alloy\nSECRET_KEY=s3cret\n",
        encoding="utf-8",
    )
    return src


def _synthetic_block(tmp_path: Path) -> Block:
    content_root = tmp_path / "block_content"
    (content_root / "backend" / "app" / "api" / "routes").mkdir(parents=True)
    src_file = content_root / "backend" / "app" / "api" / "routes" / "hello.py"
    src_file.write_text(
        "from fastapi import APIRouter\nrouter = APIRouter()\n",
        encoding="utf-8",
    )
    return Block(
        name="demo/hello",
        version="0.1.0",
        description="Test block",
        root=content_root,
        env_vars={"HELLO_SECRET": "changeme"},
        files=[
            BlockFile(src=src_file, dst="backend/app/api/routes/hello.py"),
        ],
        patches=[
            BlockPatch(
                file="backend/app/api/main.py",
                anchor="# <<ALLOY_ROUTER_REGISTER>>",
                insert=("from app.api.routes import hello\napp.include_router(hello.router)"),
            )
        ],
    )


# ── Tests ──────────────────────────────────────────────────────────


async def test_create_scaffolds_allocates_ports_and_writes_state(tmp_path: Path):
    template = _minimal_template(tmp_path)
    block = _synthetic_block(tmp_path)
    catalogue = BlockCatalogue(blocks={block.name: block})
    workspaces = tmp_path / "workspaces"

    runtime = FakeContainerRuntime()
    mgr = LocalSandboxManager(
        workspaces_root=workspaces,
        port_allocator=FixedPortAllocator([20001, 20002]),
        runtime=runtime,
    )

    info = await mgr.create(
        project_id=uuid4(),
        tenant_id="acme",
        spec=_sample_spec(),
        blocks=[block],
        catalogue=catalogue,
        base_template_dir=template,
        first_superuser_email="admin@example.com",
    )

    assert info.status == SandboxStatus.CREATED
    assert info.backend_port == 20001
    assert info.frontend_port == 20002
    assert info.preview_url == "http://localhost:20002"
    assert info.compose_project == f"alloy-{info.handle.id}"
    # State file persisted
    state_file = info.handle.workspace_path / ".alloy" / "sandbox.json"
    assert state_file.is_file()
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["id"] == info.handle.id
    assert data["backend_port"] == 20001
    # compose.alloy.yml rendered
    compose = info.handle.workspace_path / "compose.alloy.yml"
    assert compose.is_file()
    assert "alloy-" in compose.read_text(encoding="utf-8")
    # .gitignore appended
    gitignore = (info.handle.workspace_path / ".gitignore").read_text(encoding="utf-8")
    assert "compose.alloy.yml" in gitignore
    assert ".alloy/sandbox.json" in gitignore


async def test_boot_transitions_to_running_and_records_active_at(tmp_path: Path):
    template = _minimal_template(tmp_path)
    workspaces = tmp_path / "workspaces"
    runtime = FakeContainerRuntime()
    mgr = LocalSandboxManager(
        workspaces_root=workspaces,
        port_allocator=FixedPortAllocator([20001, 20002]),
        runtime=runtime,
    )

    info = await mgr.create(
        project_id=uuid4(),
        tenant_id="acme",
        spec=_sample_spec(),
        blocks=[],
        catalogue=BlockCatalogue(blocks={}),
        base_template_dir=template,
        first_superuser_email="admin@example.com",
    )

    booted = await mgr.boot(info.handle)
    assert booted.status == SandboxStatus.RUNNING
    assert booted.last_active_at is not None
    # The fake runtime recorded exactly one `up` call.
    ups = [c for c in runtime.calls if c.action == "up"]
    assert len(ups) == 1
    assert ups[0].project_name == info.compose_project


async def test_boot_transitions_to_failed_on_up_error(tmp_path: Path):
    template = _minimal_template(tmp_path)
    workspaces = tmp_path / "workspaces"
    runtime = FakeContainerRuntime()
    runtime.enqueue("up", ExecResult(returncode=1, stdout="", stderr="boom: port in use"))
    mgr = LocalSandboxManager(
        workspaces_root=workspaces,
        port_allocator=FixedPortAllocator([20001, 20002]),
        runtime=runtime,
    )
    info = await mgr.create(
        project_id=uuid4(),
        tenant_id="acme",
        spec=_sample_spec(),
        blocks=[],
        catalogue=BlockCatalogue(blocks={}),
        base_template_dir=template,
        first_superuser_email="admin@example.com",
    )
    with pytest.raises(SandboxError, match="boom"):
        await mgr.boot(info.handle)
    # State on disk reflects FAILED with the error captured.
    after = await mgr.info(info.handle)
    assert after.status == SandboxStatus.FAILED
    assert "boom" in (after.last_error or "")


async def test_archive_and_resume(tmp_path: Path):
    template = _minimal_template(tmp_path)
    workspaces = tmp_path / "workspaces"
    runtime = FakeContainerRuntime()
    mgr = LocalSandboxManager(
        workspaces_root=workspaces,
        port_allocator=FixedPortAllocator([20001, 20002]),
        runtime=runtime,
    )
    info = await mgr.create(
        project_id=uuid4(),
        tenant_id="acme",
        spec=_sample_spec(),
        blocks=[],
        catalogue=BlockCatalogue(blocks={}),
        base_template_dir=template,
        first_superuser_email="admin@example.com",
    )
    await mgr.boot(info.handle)
    archived = await mgr.archive(info.handle)
    assert archived.status == SandboxStatus.ARCHIVED
    assert archived.archived_at is not None
    assert any(c.action == "down" for c in runtime.calls)

    resumed = await mgr.resume(info.handle)
    assert resumed.status == SandboxStatus.RUNNING
    # Second up call.
    assert sum(1 for c in runtime.calls if c.action == "up") == 2


async def test_exec_refuses_when_not_running(tmp_path: Path):
    template = _minimal_template(tmp_path)
    workspaces = tmp_path / "workspaces"
    runtime = FakeContainerRuntime()
    mgr = LocalSandboxManager(
        workspaces_root=workspaces,
        port_allocator=FixedPortAllocator([20001, 20002]),
        runtime=runtime,
    )
    info = await mgr.create(
        project_id=uuid4(),
        tenant_id="acme",
        spec=_sample_spec(),
        blocks=[],
        catalogue=BlockCatalogue(blocks={}),
        base_template_dir=template,
        first_superuser_email="admin@example.com",
    )
    # Still CREATED — exec should refuse.
    with pytest.raises(SandboxError, match="must be running"):
        await mgr.exec(info.handle, "backend", ["echo", "hi"])


async def test_exec_runs_command_inside_service(tmp_path: Path):
    template = _minimal_template(tmp_path)
    workspaces = tmp_path / "workspaces"
    runtime = FakeContainerRuntime()
    runtime.enqueue("exec", ExecResult(returncode=0, stdout="hello\n", stderr=""))
    mgr = LocalSandboxManager(
        workspaces_root=workspaces,
        port_allocator=FixedPortAllocator([20001, 20002]),
        runtime=runtime,
    )
    info = await mgr.create(
        project_id=uuid4(),
        tenant_id="acme",
        spec=_sample_spec(),
        blocks=[],
        catalogue=BlockCatalogue(blocks={}),
        base_template_dir=template,
        first_superuser_email="admin@example.com",
    )
    await mgr.boot(info.handle)
    rc, out, err = await mgr.exec(info.handle, "backend", ["echo", "hi"])
    assert rc == 0
    assert out == "hello\n"
    exec_calls = [c for c in runtime.calls if c.action == "exec"]
    assert exec_calls and exec_calls[-1].extra["service"] == "backend"
    assert exec_calls[-1].extra["cmd"] == ["echo", "hi"]


async def test_destroy_frees_ports_and_removes_workspace(tmp_path: Path):
    template = _minimal_template(tmp_path)
    workspaces = tmp_path / "workspaces"
    alloc = FixedPortAllocator([20001, 20002])
    runtime = FakeContainerRuntime()
    mgr = LocalSandboxManager(
        workspaces_root=workspaces,
        port_allocator=alloc,
        runtime=runtime,
    )
    info = await mgr.create(
        project_id=uuid4(),
        tenant_id="acme",
        spec=_sample_spec(),
        blocks=[],
        catalogue=BlockCatalogue(blocks={}),
        base_template_dir=template,
        first_superuser_email="admin@example.com",
    )
    assert info.handle.workspace_path.exists()
    await mgr.destroy(info.handle)
    assert not info.handle.workspace_path.exists()
    # `docker compose down -v` recorded.
    downs = [c for c in runtime.calls if c.action == "down"]
    assert downs and downs[-1].extra["volumes"] is True


async def test_list_discovers_sandboxes_across_tenants(tmp_path: Path):
    template = _minimal_template(tmp_path)
    workspaces = tmp_path / "workspaces"
    runtime = FakeContainerRuntime()
    mgr = LocalSandboxManager(
        workspaces_root=workspaces,
        port_allocator=FixedPortAllocator([20001, 20002, 20003, 20004]),
        runtime=runtime,
    )
    await mgr.create(
        project_id=uuid4(),
        tenant_id="acme",
        spec=_sample_spec(slug="a"),
        blocks=[],
        catalogue=BlockCatalogue(blocks={}),
        base_template_dir=template,
        first_superuser_email="admin@example.com",
    )
    await mgr.create(
        project_id=uuid4(),
        tenant_id="other",
        spec=_sample_spec(slug="b"),
        blocks=[],
        catalogue=BlockCatalogue(blocks={}),
        base_template_dir=template,
        first_superuser_email="admin@example.com",
    )
    all_ = await mgr.list()
    assert len(all_) == 2
    acme = await mgr.list(tenant_id="acme")
    assert len(acme) == 1
    assert acme[0].handle.tenant_id == "acme"


async def test_state_survives_manager_restart(tmp_path: Path):
    """A fresh `LocalSandboxManager` picks up existing sandboxes.

    Validates the stateless-API design: API replicas share disk state
    and rehydrate port claims without an in-memory registry.
    """
    template = _minimal_template(tmp_path)
    workspaces = tmp_path / "workspaces"
    runtime = FakeContainerRuntime()
    mgr1 = LocalSandboxManager(
        workspaces_root=workspaces,
        port_allocator=FixedPortAllocator([20001, 20002]),
        runtime=runtime,
    )
    info = await mgr1.create(
        project_id=uuid4(),
        tenant_id="acme",
        spec=_sample_spec(),
        blocks=[],
        catalogue=BlockCatalogue(blocks={}),
        base_template_dir=template,
        first_superuser_email="admin@example.com",
    )

    # New manager instance, same root.
    mgr2 = LocalSandboxManager(
        workspaces_root=workspaces,
        port_allocator=FixedPortAllocator([20003, 20004]),
        runtime=runtime,
    )
    listed = await mgr2.list()
    assert any(i.handle.id == info.handle.id for i in listed)
    reloaded = await mgr2.info(info.handle)
    assert reloaded.backend_port == 20001
    assert reloaded.status == SandboxStatus.CREATED


async def test_load_catalogue_integration(tmp_path: Path):
    """Smoke: the sandbox manager accepts real shipped blocks.

    Ensures the `auth/clerk` + `storage/r2` manifests on disk are
    structurally compatible with the manager's `create()` signature.
    (We use the synthetic template so we don't pay the real
    `full-stack-fastapi-template` render cost here.)
    """
    template = _minimal_template(tmp_path)
    # Add anchors the real blocks expect.
    main_py = template / "backend" / "app" / "api" / "main.py"
    main_py.write_text(
        "# <<ALLOY_DEPS_IMPORT>>\n"
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n"
        "# <<ALLOY_ROUTER_REGISTER>>\n",
        encoding="utf-8",
    )
    workspaces = tmp_path / "workspaces"
    cat = load_catalogue(BLOCKS_DIR)
    blocks = [cat.get("auth/clerk"), cat.get("storage/r2")]
    mgr = LocalSandboxManager(
        workspaces_root=workspaces,
        port_allocator=FixedPortAllocator([20001, 20002]),
        runtime=FakeContainerRuntime(),
    )
    info = await mgr.create(
        project_id=uuid4(),
        tenant_id="acme",
        spec=_sample_spec(),
        blocks=blocks,
        catalogue=cat,
        base_template_dir=template,
        first_superuser_email="admin@example.com",
    )
    # Both blocks should have contributed env vars.
    env_example = (info.handle.workspace_path / ".env.example").read_text(encoding="utf-8")
    assert "CLERK_JWKS_URL" in env_example
    assert "R2_BUCKET" in env_example
