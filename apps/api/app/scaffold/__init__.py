"""Deterministic Copier-based scaffolder.

This package takes an `AppSpec` + `BuildPlan.blocks` and emits a ready-to-run
project on disk by:

1. Rendering the base template via Copier (`run_copy`) with answers derived
   from the spec.
2. Overlaying each block's `content/` tree onto the rendered project.
3. Applying each block's anchor-based patches.
4. Appending each block's env vars to `.env.example` and Python/JS dependencies
   to `pyproject.toml` / `package.json`.
5. Writing `.alloy/manifest.json` recording the exact base version + block
   versions — required for `copier update` and for the "template update
   available" PR flow in Phase 4.

Design notes:

* The scaffolder is pure — no LLM, no network, no Alloy DB access. It takes
  inputs and paths and returns a report. The route layer calls this inside a
  thread executor because Copier's API is synchronous.
* Anchor patches are intentionally *string-based* at this stage. Phase 2's
  visual picker brings a tree-sitter layer for AST-aware patches; the anchor
  format survives either way because it's a comment in the source.
* Blocks are loaded from the repo-level `blocks/` directory rather than from
  the api package so block authors can edit YAML + content without rebuilding
  the API image.
"""

from app.scaffold.blocks import Block, BlockCatalogue, BlockError, load_catalogue
from app.scaffold.scaffolder import ScaffoldError, ScaffoldReport, scaffold_project

__all__ = [
    "Block",
    "BlockCatalogue",
    "BlockError",
    "ScaffoldError",
    "ScaffoldReport",
    "load_catalogue",
    "scaffold_project",
]
