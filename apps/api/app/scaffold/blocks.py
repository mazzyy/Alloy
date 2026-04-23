"""Block loading + validation.

A block is a directory under `blocks/<kind>/<name>/` with a `block.yaml`
manifest and a `content/` subtree of files to overlay into a generated
project. This module is responsible for:

* discovering blocks from the repo
* validating manifests (required fields, conflict declarations)
* exposing a typed `Block` object the scaffolder can apply

Keeping validation centralized here means the Planner's block catalogue and
the Scaffolder's applier cannot drift — both call `load_catalogue()` and get
the same `Block` instances.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class BlockError(RuntimeError):
    """Raised when a block manifest is malformed or a named block is missing."""


@dataclass(frozen=True)
class BlockFile:
    src: Path  # absolute path inside blocks/<kind>/<name>/content
    dst: str  # relative to the generated project root


@dataclass(frozen=True)
class BlockPatch:
    file: str  # relative to generated project root
    anchor: str  # substring to locate in the target file
    insert: str  # text to insert on the line *after* the anchor


@dataclass(frozen=True)
class Block:
    """Validated, in-memory representation of a block."""

    name: str  # e.g. "auth/clerk"
    version: str
    description: str
    root: Path  # absolute path to the block directory
    python_dependencies: list[str] = field(default_factory=list)
    js_dependencies: dict[str, str] = field(default_factory=dict)
    env_vars: dict[str, str] = field(default_factory=dict)
    files: list[BlockFile] = field(default_factory=list)
    patches: list[BlockPatch] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BlockCatalogue:
    """A read-only view of the block library."""

    blocks: dict[str, Block]

    def get(self, name: str) -> Block:
        try:
            return self.blocks[name]
        except KeyError as exc:
            raise BlockError(f"Unknown block: {name!r}") from exc

    def get_many(self, names: list[str]) -> list[Block]:
        return [self.get(n) for n in names]

    def assert_no_conflicts(self, names: list[str]) -> None:
        """Error if any selected block declares a conflict with another selected."""
        selected = set(names)
        for n in names:
            block = self.get(n)
            clash = selected.intersection(block.conflicts)
            if clash:
                raise BlockError(f"Block {n!r} conflicts with selected block(s): {sorted(clash)!r}")


def _parse_manifest(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            parsed = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise BlockError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise BlockError(f"Manifest {path} is not a YAML mapping")
    for required in ("name", "version"):
        if required not in parsed:
            raise BlockError(f"Manifest {path} missing required field {required!r}")
    return parsed


def _build_block(manifest: dict[str, Any], block_dir: Path) -> Block:
    name = str(manifest["name"])
    version = str(manifest["version"])

    files: list[BlockFile] = []
    for entry in manifest.get("files") or []:
        src_rel = str(entry["src"])
        dst = str(entry["dst"])
        src_abs = (block_dir / src_rel).resolve()
        if not src_abs.exists():
            raise BlockError(f"Block {name!r}: file {src_rel!r} does not exist at {src_abs}")
        files.append(BlockFile(src=src_abs, dst=dst))

    patches: list[BlockPatch] = []
    for entry in manifest.get("patches") or []:
        patches.append(
            BlockPatch(
                file=str(entry["file"]),
                anchor=str(entry["anchor"]),
                insert=str(entry["insert"]),
            )
        )

    js_raw = manifest.get("js_dependencies") or {}
    if not isinstance(js_raw, dict):
        raise BlockError(
            f"Block {name!r}: js_dependencies must be a mapping, got {type(js_raw).__name__}"
        )

    env_raw = manifest.get("env_vars") or {}
    if not isinstance(env_raw, dict):
        raise BlockError(f"Block {name!r}: env_vars must be a mapping")

    return Block(
        name=name,
        version=version,
        description=str(manifest.get("description") or "").strip(),
        root=block_dir,
        python_dependencies=list(manifest.get("python_dependencies") or []),
        js_dependencies={str(k): str(v) for k, v in js_raw.items()},
        env_vars={str(k): str(v) for k, v in env_raw.items()},
        files=files,
        patches=patches,
        conflicts=list(manifest.get("conflicts") or []),
    )


def load_catalogue(blocks_root: Path) -> BlockCatalogue:
    """Walk `blocks_root` and load every `block.yaml` found two levels deep.

    Layout: `blocks/<kind>/<name>/block.yaml`. We expect the name inside the
    manifest to match `<kind>/<name>` — otherwise we refuse to load to avoid
    silent aliasing bugs.
    """
    if not blocks_root.is_dir():
        raise BlockError(f"Blocks root {blocks_root} is not a directory")

    loaded: dict[str, Block] = {}
    for manifest_path in sorted(blocks_root.glob("*/*/block.yaml")):
        manifest = _parse_manifest(manifest_path)
        block_dir = manifest_path.parent
        # Expected name derived from directory structure.
        expected = f"{block_dir.parent.name}/{block_dir.name}"
        if manifest["name"] != expected:
            raise BlockError(
                f"Manifest name {manifest['name']!r} in {manifest_path} does not match "
                f"expected directory-derived name {expected!r}"
            )
        block = _build_block(manifest, block_dir)
        if block.name in loaded:
            raise BlockError(f"Duplicate block name {block.name!r}")
        loaded[block.name] = block
    return BlockCatalogue(blocks=loaded)
