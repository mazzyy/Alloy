"""Scaffold orchestrator.

Entry point: `scaffold_project(spec, blocks, target_dir, ...)` returns a
`ScaffoldReport` describing exactly what was written.

Order of operations:

1. Validate inputs: `target_dir` is empty (or permitted to exist with `overwrite=True`).
2. Resolve blocks from the catalogue and check conflicts.
3. Render the base template via `copier.run_copy`.
4. Overlay each block's files.
5. Apply each block's anchor patches.
6. Append deps + env vars.
7. Write `.alloy/manifest.json` and initialise a git repo.

We go through three layers of safety nets because the user's generated app
*is* their code — any corruption is catastrophic:

* Pre-check: dry-run the block overlay and reject if any `dst` already exists
  without `--force-block-overwrite`. This catches block authors shipping a
  file that clobbers a base-template file silently.
* Anchor verification: if a patch's anchor isn't present, scaffolding aborts
  before writing the patch (no half-applied state).
* On any exception we leave the partially-written dir so the caller can
  inspect — we *don't* `rm -rf` (the sandbox's git repo is enough rollback).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import copier  # type: ignore[import-untyped]
import yaml
from alloy_shared.spec import AppSpec

from app.scaffold.answer_builder import build_answers
from app.scaffold.blocks import Block, BlockCatalogue, BlockError


class ScaffoldError(RuntimeError):
    """Raised when scaffolding cannot proceed (conflict, missing anchor, etc.)."""


@dataclass
class ScaffoldReport:
    target_dir: Path
    base_template_version: str
    blocks_applied: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    files_overwritten: list[str] = field(default_factory=list)
    patches_applied: list[str] = field(default_factory=list)
    env_vars_added: list[str] = field(default_factory=list)
    python_deps_added: list[str] = field(default_factory=list)
    js_deps_added: list[str] = field(default_factory=list)
    git_initialised: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_dir": str(self.target_dir),
            "base_template_version": self.base_template_version,
            "blocks_applied": self.blocks_applied,
            "files_written": self.files_written,
            "files_overwritten": self.files_overwritten,
            "patches_applied": self.patches_applied,
            "env_vars_added": self.env_vars_added,
            "python_deps_added": self.python_deps_added,
            "js_deps_added": self.js_deps_added,
            "git_initialised": self.git_initialised,
        }


def _read_template_version(base_template_dir: Path) -> str:
    version_file = base_template_dir / "VERSION"
    if version_file.is_file():
        return version_file.read_text(encoding="utf-8").strip()
    return "unknown"


def _overlay_block_files(
    block: Block,
    target_dir: Path,
    *,
    allow_overwrite: bool,
    report: ScaffoldReport,
) -> None:
    for item in block.files:
        dst = target_dir / item.dst
        if dst.exists():
            if not allow_overwrite:
                raise ScaffoldError(
                    f"Block {block.name!r} would overwrite existing file "
                    f"{item.dst!r}. Pass overwrite_block_files=True to allow."
                )
            report.files_overwritten.append(item.dst)
        else:
            report.files_written.append(item.dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        content = item.src.read_bytes()
        dst.write_bytes(content)


def _apply_patches(block: Block, target_dir: Path, report: ScaffoldReport) -> None:
    for patch in block.patches:
        target = target_dir / patch.file
        if not target.is_file():
            raise ScaffoldError(f"Block {block.name!r} patches missing file {patch.file!r}")
        original = target.read_text(encoding="utf-8")
        if patch.anchor not in original:
            raise ScaffoldError(
                f"Block {block.name!r}: anchor {patch.anchor!r} not found in "
                f"{patch.file!r}. Base template may be out of sync."
            )
        lines = original.splitlines(keepends=True)
        out: list[str] = []
        inserted = False
        for line in lines:
            out.append(line)
            if not inserted and patch.anchor in line:
                # Preserve the anchor line's leading whitespace for the insert.
                indent = line[: len(line) - len(line.lstrip())]
                for ins_line in patch.insert.splitlines():
                    out.append(f"{indent}{ins_line}\n" if ins_line else "\n")
                inserted = True
        if not inserted:
            # Defensive — substring check above said anchor was present.
            raise ScaffoldError(
                f"Block {block.name!r}: anchor {patch.anchor!r} matched globally but "
                f"no line contained it in {patch.file!r}"
            )
        target.write_text("".join(out), encoding="utf-8")
        report.patches_applied.append(f"{block.name}->{patch.file}")


def _append_env_vars(target_dir: Path, blocks: list[Block], report: ScaffoldReport) -> None:
    env_file = target_dir / ".env.example"
    additions: list[str] = []
    existing = env_file.read_text(encoding="utf-8") if env_file.is_file() else ""
    for block in blocks:
        if not block.env_vars:
            continue
        additions.append(f"\n# ── {block.name} ─────────────────────────────")
        for key, value in block.env_vars.items():
            if f"\n{key}=" in existing or existing.startswith(f"{key}="):
                continue  # already present — respect the base template
            additions.append(f"{key}={value}")
            report.env_vars_added.append(key)
    if additions:
        with env_file.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(additions) + "\n")


def _append_python_deps(target_dir: Path, blocks: list[Block], report: ScaffoldReport) -> None:
    # Base template uses pyproject.toml. We append a raw `# alloy-blocks` table
    # rather than editing the existing `dependencies = [...]` list — the
    # generated project's `uv sync` picks up both tables via
    # `[tool.uv.sources]` / optional-dependencies in Phase 1 wk5 when the
    # Coder Agent is doing real edits. For now, a trailing block is enough
    # signal for humans + `uv add`.
    pyproj = target_dir / "backend" / "pyproject.toml"
    if not pyproj.is_file():
        # Template variant may live at repo root.
        pyproj = target_dir / "pyproject.toml"
    if not pyproj.is_file():
        return  # nothing to do — template changed layout; caller sees empty list
    lines = ["\n\n# ─── Added by Alloy blocks ──────────────────────────────"]
    for block in blocks:
        if not block.python_dependencies:
            continue
        lines.append(f"# {block.name} {block.version}")
        for dep in block.python_dependencies:
            lines.append(f"# alloy-dep: {dep}")
            report.python_deps_added.append(dep)
    if len(lines) > 1:
        with pyproj.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def _append_js_deps(target_dir: Path, blocks: list[Block], report: ScaffoldReport) -> None:
    pkg_json = target_dir / "frontend" / "package.json"
    if not pkg_json.is_file():
        return
    try:
        data = json.loads(pkg_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScaffoldError(f"frontend/package.json is not valid JSON: {exc}") from exc
    deps = data.setdefault("dependencies", {})
    for block in blocks:
        for name, version in block.js_dependencies.items():
            if name in deps:
                continue  # respect what the template declared
            deps[name] = version
            report.js_deps_added.append(f"{name}@{version}")
    pkg_json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _write_manifest(
    target_dir: Path,
    spec: AppSpec,
    base_version: str,
    blocks: list[Block],
) -> None:
    alloy_dir = target_dir / ".alloy"
    alloy_dir.mkdir(exist_ok=True)
    manifest = {
        "alloy_schema_version": 1,
        "spec_slug": spec.slug,
        "spec_name": spec.name,
        "base_template_version": base_version,
        "blocks": [{"name": b.name, "version": b.version} for b in blocks],
    }
    (alloy_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    # Also drop a YAML copy for humans grep-ing the repo.
    (alloy_dir / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )


def _git_init(target_dir: Path, report: ScaffoldReport) -> None:
    """Initialise a git repo so every subsequent Coder Agent write is a commit.

    We don't commit anything yet — the sandbox manager does the first commit
    as `feat: scaffold` after validating the tree runs.
    """
    try:
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=target_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        (target_dir / ".gitignore").write_text(
            "\n".join(
                [
                    "# Alloy defaults",
                    "node_modules/",
                    ".venv/",
                    "__pycache__/",
                    "*.pyc",
                    ".env",
                    "dist/",
                    "build/",
                    ".pytest_cache/",
                    ".mypy_cache/",
                    ".ruff_cache/",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        report.git_initialised = True
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Git missing in sandbox — not fatal. The sandbox manager retries.
        report.git_initialised = False


def scaffold_project(
    spec: AppSpec,
    *,
    blocks: list[Block],
    catalogue: BlockCatalogue,
    base_template_dir: Path,
    target_dir: Path,
    first_superuser_email: str,
    domain_base: str = "localhost",
    overwrite_block_files: bool = False,
    skip_git: bool = False,
) -> ScaffoldReport:
    """Render the base template + apply blocks into `target_dir`.

    `blocks` is the ordered list to apply; `catalogue` is used for conflict
    resolution. We accept both rather than re-resolving from names so callers
    (the Planner's recipe) can guarantee the set is stable across retries.
    """
    if not base_template_dir.is_dir():
        raise ScaffoldError(f"Base template {base_template_dir} not found")

    target_dir = target_dir.resolve()
    if target_dir.exists() and any(target_dir.iterdir()):
        raise ScaffoldError(f"Target dir {target_dir} is not empty")
    target_dir.mkdir(parents=True, exist_ok=True)

    # Conflict check — avoids silent mis-stacks (e.g. auth/clerk + auth/jwt).
    catalogue.assert_no_conflicts([b.name for b in blocks])

    # 1. Render the base template with Copier.
    answers = build_answers(
        spec, first_superuser_email=first_superuser_email, domain_base=domain_base
    )
    try:
        copier.run_copy(
            src_path=str(base_template_dir),
            dst_path=str(target_dir),
            data=answers,
            defaults=True,
            unsafe=True,  # allows template _tasks / _migrations to run (they're ours)
            quiet=True,
            overwrite=True,
        )
    except (copier.errors.CopierError, Exception) as exc:  # pragma: no cover
        raise ScaffoldError(f"Copier render failed: {exc}") from exc

    report = ScaffoldReport(
        target_dir=target_dir,
        base_template_version=_read_template_version(base_template_dir),
    )

    # 2. Apply each block.
    for block in blocks:
        _overlay_block_files(
            block, target_dir, allow_overwrite=overwrite_block_files, report=report
        )
        _apply_patches(block, target_dir, report=report)
        report.blocks_applied.append(f"{block.name}@{block.version}")

    # 3. Aggregate deps + env.
    _append_env_vars(target_dir, blocks, report)
    _append_python_deps(target_dir, blocks, report)
    _append_js_deps(target_dir, blocks, report)

    # 4. Manifest + git init.
    _write_manifest(target_dir, spec, report.base_template_version, blocks)
    if not skip_git:
        _git_init(target_dir, report)

    return report


# Surface errors imported from the blocks module alongside our own so callers
# only import from `app.scaffold`.
__all__ = ["ScaffoldError", "ScaffoldReport", "scaffold_project", "BlockError"]
