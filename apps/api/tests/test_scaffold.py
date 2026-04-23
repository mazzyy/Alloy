"""Tests for the deterministic scaffolder.

Strategy:

* `load_catalogue` is tested directly against the real `blocks/` dir.
* `build_answers` is tested with various `AppSpec`s.
* `scaffold_project` is tested with a **minimal synthetic template** so the
  unit tests don't pay the cost of rendering the ~500-file base template.
  The base template gets a light smoke-test at the end that just checks
  copier.run_copy doesn't explode (skipped when the VERSION file is absent).
"""

from __future__ import annotations

from pathlib import Path

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

from app.scaffold import (
    BlockError,
    ScaffoldError,
    load_catalogue,
    scaffold_project,
)
from app.scaffold.answer_builder import _slugify_stack, build_answers
from app.scaffold.blocks import Block, BlockCatalogue, BlockFile, BlockPatch

REPO_ROOT = Path(__file__).resolve().parents[3]
BLOCKS_DIR = REPO_ROOT / "blocks"


# ── Helpers ────────────────────────────────────────────────────────────────


def _sample_spec(slug: str = "task-tracker") -> AppSpec:
    return AppSpec(
        name="Task Tracker",
        slug=slug,
        description="Tracks tasks across a team.",
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
                    EntityField(
                        name="title",
                        type="string",
                        required=True,
                        unique=False,
                        indexed=False,
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
    """Build a synthetic Copier template with a single anchor-marked file."""
    src = tmp_path / "template"
    src.mkdir()

    (src / "copier.yml").write_text(
        "project_name:\n"
        "  type: str\n"
        "  default: My App\n"
        "stack_name:\n"
        "  type: str\n"
        "  default: my-app\n",
        encoding="utf-8",
    )
    (src / "VERSION").write_text("1.0.0-test", encoding="utf-8")

    # One templated file + one anchor target.
    (src / "README.md.jinja").write_text(
        "# {{ project_name }}\nstack: {{ stack_name }}\n", encoding="utf-8"
    )
    backend_dir = src / "backend" / "app" / "api"
    backend_dir.mkdir(parents=True)
    (backend_dir / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "\n"
        "app = FastAPI()\n"
        "\n"
        "# <<ALLOY_ROUTER_REGISTER>>\n"
        "\n",
        encoding="utf-8",
    )
    frontend_pkg = src / "frontend"
    frontend_pkg.mkdir()
    (frontend_pkg / "package.json").write_text(
        '{\n  "name": "scaffold-test",\n  "dependencies": {}\n}\n',
        encoding="utf-8",
    )
    (src / ".env.example").write_text("EXISTING_VAR=present\n", encoding="utf-8")
    return src


def _synthetic_block(tmp_path: Path) -> Block:
    """A small in-memory block that lays one file + one anchor patch."""
    content_root = tmp_path / "block_content"
    (content_root / "backend" / "app" / "api" / "routes").mkdir(parents=True)
    src_file = content_root / "backend" / "app" / "api" / "routes" / "hello.py"
    src_file.write_text("from fastapi import APIRouter\nrouter = APIRouter()\n")
    return Block(
        name="demo/hello",
        version="0.1.0",
        description="Test block",
        root=content_root,
        python_dependencies=["rich>=13.0.0"],
        js_dependencies={"dayjs": "^1.11.0"},
        env_vars={"HELLO_SECRET": "changeme"},
        files=[
            BlockFile(
                src=src_file,
                dst="backend/app/api/routes/hello.py",
            )
        ],
        patches=[
            BlockPatch(
                file="backend/app/api/main.py",
                anchor="# <<ALLOY_ROUTER_REGISTER>>",
                insert="from app.api.routes import hello\napp.include_router(hello.router)",
            )
        ],
    )


# ── load_catalogue ─────────────────────────────────────────────────────────


def test_load_catalogue_discovers_shipped_blocks():
    cat = load_catalogue(BLOCKS_DIR)
    assert "auth/clerk" in cat.blocks
    assert "storage/r2" in cat.blocks

    clerk = cat.get("auth/clerk")
    assert clerk.version
    assert any("pyjwt" in dep for dep in clerk.python_dependencies)
    assert "@clerk/clerk-react" in clerk.js_dependencies


def test_load_catalogue_rejects_mismatched_name(tmp_path: Path):
    bad = tmp_path / "blocks" / "kind" / "name"
    bad.mkdir(parents=True)
    (bad / "block.yaml").write_text("name: kind/different\nversion: 0.1.0\n", encoding="utf-8")
    with pytest.raises(BlockError, match="does not match expected"):
        load_catalogue(tmp_path / "blocks")


def test_catalogue_conflict_detection():
    cat = load_catalogue(BLOCKS_DIR)
    # auth/clerk declares conflicts with auth/fastapi_users_jwt +
    # auth/custom_jwt. We fake their presence by extending the catalogue.
    extra = Block(
        name="auth/custom_jwt",
        version="0.1.0",
        description="",
        root=BLOCKS_DIR,  # placeholder — unused here
    )
    extended = BlockCatalogue(blocks={**cat.blocks, "auth/custom_jwt": extra})
    with pytest.raises(BlockError, match="conflicts with"):
        extended.assert_no_conflicts(["auth/clerk", "auth/custom_jwt"])


# ── build_answers ──────────────────────────────────────────────────────────


def test_slugify_stack_is_docker_compose_label_safe():
    # AppSpec.slug has a regex constraint (^[a-z][a-z0-9-]*$) so we test the
    # helper directly — it must also tolerate messier input in case we ever
    # source the stack name from something less strict (user-facing name, say).
    cases = {
        "task-tracker": "task-tracker",
        "Task Tracker / Pro": "task-tracker---pro",  # hyphen-coalesced is fine for compose
        "---weird---": "weird",
        "": "alloy-app",
    }
    for raw, expected in cases.items():
        got = _slugify_stack(raw)
        assert " " not in got, raw
        assert "/" not in got, raw
        assert got == expected, (raw, got, expected)


def test_build_answers_stack_name_respects_slug():
    spec = _sample_spec(slug="my-tracker")
    answers = build_answers(spec, first_superuser_email="demo@example.com")
    assert answers["stack_name"] == "my-tracker"


def test_build_answers_generates_distinct_secrets():
    spec = _sample_spec()
    a = build_answers(spec, first_superuser_email="a@example.com")
    b = build_answers(spec, first_superuser_email="a@example.com")
    # Secrets should be different across renders (one-shot per project).
    assert a["secret_key"] != b["secret_key"]
    assert a["postgres_password"] != b["postgres_password"]


def test_build_answers_populates_required_fields():
    spec = _sample_spec()
    answers = build_answers(spec, first_superuser_email="admin@example.com")
    required = {
        "project_name",
        "stack_name",
        "secret_key",
        "first_superuser",
        "first_superuser_password",
        "postgres_password",
        "emails_from_email",
        "docker_image_backend",
        "docker_image_frontend",
    }
    assert required.issubset(answers.keys())


# ── scaffold_project (synthetic template) ──────────────────────────────────


def test_scaffold_renders_template_and_applies_block(tmp_path: Path):
    template = _minimal_template(tmp_path)
    target = tmp_path / "out"
    block = _synthetic_block(tmp_path)
    catalogue = BlockCatalogue(blocks={block.name: block})
    spec = _sample_spec()

    report = scaffold_project(
        spec,
        blocks=[block],
        catalogue=catalogue,
        base_template_dir=template,
        target_dir=target,
        first_superuser_email="test@example.com",
        skip_git=True,
    )

    # 1. Base template rendered.
    readme = (target / "README.md").read_text(encoding="utf-8")
    assert "Task Tracker" in readme

    # 2. Block file laid down.
    hello = target / "backend" / "app" / "api" / "routes" / "hello.py"
    assert hello.exists()
    assert "APIRouter" in hello.read_text(encoding="utf-8")

    # 3. Anchor patch applied (text inserted right after the anchor line).
    main_py = (target / "backend" / "app" / "api" / "main.py").read_text(encoding="utf-8")
    anchor_idx = main_py.index("# <<ALLOY_ROUTER_REGISTER>>")
    after = main_py[anchor_idx:]
    assert "from app.api.routes import hello" in after
    assert "app.include_router(hello.router)" in after

    # 4. Env + deps appended.
    env = (target / ".env.example").read_text(encoding="utf-8")
    assert "HELLO_SECRET=changeme" in env
    assert "EXISTING_VAR=present" in env  # base template untouched

    pkg = (target / "frontend" / "package.json").read_text(encoding="utf-8")
    assert "dayjs" in pkg

    # 5. Manifest recorded.
    manifest = (target / ".alloy" / "manifest.json").read_text(encoding="utf-8")
    assert "demo/hello" in manifest
    assert "1.0.0-test" in manifest

    # 6. Report is accurate.
    assert "demo/hello@0.1.0" in report.blocks_applied
    assert "backend/app/api/routes/hello.py" in report.files_written
    assert any("demo/hello" in p for p in report.patches_applied)
    assert "HELLO_SECRET" in report.env_vars_added


def test_scaffold_refuses_non_empty_target(tmp_path: Path):
    template = _minimal_template(tmp_path)
    target = tmp_path / "out"
    target.mkdir()
    (target / "stray.txt").write_text("exists")
    catalogue = BlockCatalogue(blocks={})
    with pytest.raises(ScaffoldError, match="not empty"):
        scaffold_project(
            _sample_spec(),
            blocks=[],
            catalogue=catalogue,
            base_template_dir=template,
            target_dir=target,
            first_superuser_email="x@y.z",
            skip_git=True,
        )


def test_scaffold_aborts_on_missing_anchor(tmp_path: Path):
    template = _minimal_template(tmp_path)
    # Remove the anchor from the rendered target to simulate a drifted template.
    main_py = template / "backend" / "app" / "api" / "main.py"
    main_py.write_text(
        main_py.read_text(encoding="utf-8").replace(
            "# <<ALLOY_ROUTER_REGISTER>>", "# (anchor removed)"
        ),
        encoding="utf-8",
    )
    target = tmp_path / "out"
    block = _synthetic_block(tmp_path)
    catalogue = BlockCatalogue(blocks={block.name: block})

    with pytest.raises(ScaffoldError, match="anchor.*not found"):
        scaffold_project(
            _sample_spec(),
            blocks=[block],
            catalogue=catalogue,
            base_template_dir=template,
            target_dir=target,
            first_superuser_email="x@y.z",
            skip_git=True,
        )


def test_scaffold_refuses_conflicting_blocks(tmp_path: Path):
    template = _minimal_template(tmp_path)
    target = tmp_path / "out"
    # Two blocks with mutual conflict.
    block_a = Block(
        name="auth/a",
        version="0.1.0",
        description="",
        root=tmp_path,
        conflicts=["auth/b"],
    )
    block_b = Block(
        name="auth/b",
        version="0.1.0",
        description="",
        root=tmp_path,
        conflicts=["auth/a"],
    )
    catalogue = BlockCatalogue(blocks={block_a.name: block_a, block_b.name: block_b})
    with pytest.raises(BlockError, match="conflicts"):
        scaffold_project(
            _sample_spec(),
            blocks=[block_a, block_b],
            catalogue=catalogue,
            base_template_dir=template,
            target_dir=target,
            first_superuser_email="x@y.z",
            skip_git=True,
        )
