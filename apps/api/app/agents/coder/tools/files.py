"""Filesystem tools: `list_files`, `read_file`, `write_file`.

These run on the *host* filesystem against the sandbox's workspace dir,
not inside a container — sandboxes mount the workspace at a known path
so reads/writes from the gateway side stay consistent with what's
visible inside the backend container. Commands that need the container
(pytest, alembic, etc.) go through `commands.py` / `validators.py` and
round-trip via `SandboxManager.exec()`.

Per-call caps to protect the model's context window:

* `list_files`: 500 entries max (rest → `truncated=True`)
* `read_file`: 2_000 lines max per call; larger files require paged reads
* `write_file`: refuses to overwrite existing files — forces the agent
  to use `apply_patch` for edits, which keeps diffs auditable
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai import RunContext

from app.agents.coder.context import CoderDeps
from app.agents.coder.errors import ToolInputError
from app.agents.coder.results import FileList, FileListEntry, FileRead, WriteResult
from app.agents.coder.tools._paths import rel_to, resolve_inside

if TYPE_CHECKING:
    from pydantic_ai import Agent


# How many entries a single `list_files` call may return. Above this, we
# truncate and set `truncated=True`. 500 is enough to list a typical
# feature directory while keeping the tool return under ~30 KB.
_LIST_CAP = 500

# Max lines we'll hand back from `read_file` in one shot. Files bigger
# than this need to be read in windows (start_line/end_line).
_READ_LINE_CAP = 2_000

# Directories we never descend into when listing. Not a security
# boundary — the agent *can* read them if it asks by name — just a
# relevance filter so the tree stays readable.
_LIST_SKIP = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".next",
        ".turbo",
    }
)


def _list_dir(root: Path, rel: str, pattern: str | None) -> FileList:
    base = resolve_inside(root, rel or ".")
    if not base.exists():
        raise ToolInputError(f"path does not exist: {rel!r}")
    if not base.is_dir():
        raise ToolInputError(f"path is not a directory: {rel!r}")

    entries: list[FileListEntry] = []
    truncated = False

    # Walk shallowly; recursion is opt-in via the glob pattern.
    # If the model wants `src/**/*.py` it gets a deep walk with fnmatch.
    if pattern and "**" in pattern:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in _LIST_SKIP)
            for name in sorted(filenames):
                full = Path(dirpath) / name
                relp = rel_to(root, full)
                if not fnmatch.fnmatch(relp, pattern):
                    continue
                if len(entries) >= _LIST_CAP:
                    truncated = True
                    break
                try:
                    size = full.stat().st_size
                except OSError:
                    size = None
                entries.append(FileListEntry(path=relp, is_dir=False, size_bytes=size))
            if truncated:
                break
    else:
        for child in sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
            if child.name in _LIST_SKIP:
                continue
            relp = rel_to(root, child)
            if pattern and not fnmatch.fnmatch(child.name, pattern):
                continue
            if len(entries) >= _LIST_CAP:
                truncated = True
                break
            size: int | None = None
            is_dir = child.is_dir()
            if not is_dir:
                try:
                    size = child.stat().st_size
                except OSError:
                    size = None
            entries.append(FileListEntry(path=relp, is_dir=is_dir, size_bytes=size))

    return FileList(
        root=rel_to(root, base) or ".",
        entries=entries,
        truncated=truncated,
    )


def _read_file(
    root: Path,
    rel: str,
    start_line: int | None,
    end_line: int | None,
) -> FileRead:
    path = resolve_inside(root, rel)
    if not path.exists():
        raise ToolInputError(f"file does not exist: {rel!r}")
    if path.is_dir():
        raise ToolInputError(f"path is a directory, not a file: {rel!r}")

    # Binary-ish guard: refuse NUL bytes in the first 4 KB. The model
    # shouldn't ask to read .png / .pdf from the scaffold.
    with path.open("rb") as fp:
        head = fp.read(4096)
    if b"\x00" in head:
        raise ToolInputError(f"refusing to read binary file: {rel!r}")

    # Validate line window before reading the full file.
    if start_line is not None and start_line < 1:
        raise ToolInputError(f"start_line must be >= 1, got {start_line}")
    if end_line is not None and end_line < 1:
        raise ToolInputError(f"end_line must be >= 1, got {end_line}")
    if start_line and end_line and end_line < start_line:
        raise ToolInputError(
            f"end_line ({end_line}) must be >= start_line ({start_line})"
        )

    text = path.read_text(encoding="utf-8", errors="replace")
    # Preserve trailing newline state by keeping the split semantic.
    lines = text.splitlines(keepends=True)
    total = len(lines)

    s = (start_line or 1) - 1
    e = end_line if end_line is not None else total
    e = min(e, total)
    if s < 0:
        s = 0

    # Cap slice to _READ_LINE_CAP lines regardless of end_line.
    clipped = False
    if e - s > _READ_LINE_CAP:
        e = s + _READ_LINE_CAP
        clipped = True

    sliced = "".join(lines[s:e])
    return FileRead(
        path=rel_to(root, path),
        content=sliced,
        start_line=s + 1,
        end_line=e,
        line_count=total,
        clipped=clipped,
    )


def _write_file(root: Path, rel: str, content: str) -> WriteResult:
    if not rel or not rel.strip():
        raise ToolInputError("path must not be empty")
    path = resolve_inside(root, rel)
    if path.exists():
        # Explicit: write_file is create-only. Edits must go through
        # apply_patch so diffs stay auditable.
        raise ToolInputError(
            f"file already exists: {rel!r}; use apply_patch to edit existing files"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8")
    path.write_bytes(data)
    return WriteResult(path=rel_to(root, path), bytes_written=len(data), created=True)


def register(agent: "Agent[CoderDeps, str]") -> None:
    """Attach `list_files`, `read_file`, `write_file` to `agent`."""

    @agent.tool
    async def list_files(
        ctx: "RunContext[CoderDeps]",
        path: str = ".",
        pattern: str | None = None,
    ) -> FileList:
        """List files under `path` (relative to the workspace root).

        `pattern` is an fnmatch glob. If it contains `**`, a recursive
        walk runs; otherwise we match against filenames in the immediate
        directory only. Results are capped at 500 entries.
        """
        ctx.deps.bind(tool="list_files", path=path, pattern=pattern).debug("coder.tool")
        return _list_dir(ctx.deps.workspace_root, path, pattern)

    @agent.tool
    async def read_file(
        ctx: "RunContext[CoderDeps]",
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> FileRead:
        """Return the contents of `path` (optionally a 1-indexed line range).

        If the window exceeds 2 000 lines we return the first 2 000 and
        set `clipped=True` — the agent should page by bumping start_line.
        """
        ctx.deps.bind(
            tool="read_file", path=path, start_line=start_line, end_line=end_line
        ).debug("coder.tool")
        return _read_file(ctx.deps.workspace_root, path, start_line, end_line)

    @agent.tool
    async def write_file(
        ctx: "RunContext[CoderDeps]",
        path: str,
        content: str,
    ) -> WriteResult:
        """Create a new file at `path` with `content`.

        Refuses to overwrite existing files — use `apply_patch` to edit.
        Creates parent directories as needed.
        """
        ctx.deps.bind(tool="write_file", path=path, bytes=len(content)).debug("coder.tool")
        result = _write_file(ctx.deps.workspace_root, path, content)
        # Record the write so the validator loop can scope lint/type
        # checks to files we actually touched this turn.
        ctx.deps.touched_paths.add(result.path)
        return result
