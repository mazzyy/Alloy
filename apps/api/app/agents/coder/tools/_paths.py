"""Shared path-safety helpers for every tool that touches the filesystem.

All tools accept paths *relative to the workspace root* — the agent
should never see or be able to name an absolute path. `resolve_inside()`
rejects:

* Absolute paths (`/etc/passwd`)
* Parent traversal (`../outside`)
* Symlinks that escape the workspace (a symlink's *target* must also
  resolve inside the root)
* NUL bytes and other shenanigans

Returns a resolved absolute `Path` that callers can safely pass to
open() / shutil / subprocess.
"""

from __future__ import annotations

from pathlib import Path

from app.agents.coder.errors import ToolInputError, WorkspaceEscapeError


def resolve_inside(root: Path, rel: str) -> Path:
    """Resolve `rel` against `root`, raising if the result escapes.

    Called by every FS-touching tool. `rel` comes from the LLM; treat it
    as untrusted.
    """
    if not rel or not rel.strip():
        raise ToolInputError("path must be a non-empty string")
    if "\x00" in rel:
        raise ToolInputError("path must not contain NUL bytes")

    # Reject absolute paths up front — a well-behaved agent never needs
    # one, and the error message is clearer than a silent `root / "/etc"`
    # collapse.
    candidate = Path(rel)
    if candidate.is_absolute():
        raise WorkspaceEscapeError(f"absolute paths are not allowed: {rel}")

    resolved_root = root.expanduser().resolve()
    # `.resolve()` follows symlinks; that's what we want — a symlink out
    # of the workspace still fails the containment check.
    resolved = (resolved_root / candidate).resolve()

    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise WorkspaceEscapeError(
            f"path escapes workspace root: {rel!r} → {resolved}"
        ) from exc

    return resolved


def rel_to(root: Path, absolute: Path) -> str:
    """Inverse of `resolve_inside` — stringify relative to `root`.

    Used by results so the LLM only ever sees workspace-relative paths.
    """
    try:
        return absolute.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        # Shouldn't happen if the path originated from resolve_inside,
        # but be defensive: surface it as an unsafe path rather than
        # leak the absolute path to the model.
        return "(outside-workspace)"
