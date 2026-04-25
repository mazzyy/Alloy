"""`apply_patch` — the preferred mechanism for editing existing files.

**Why not use `patch(1)` or `git apply`?** Both require a correctly
numbered unified-diff header (`@@ -a,b +c,d @@`) with exact surrounding
context. LLMs emit diffs with the right *shape* but often wrong line
numbers and drifted context, especially after auto-formatting. A naive
`git apply` refuses them; `patch -p0 --fuzz=3` accepts them silently
but mis-applies when two near-duplicate hunks exist.

Our approach is a hand-rolled two-tier applier:

1. **Exact anchor match.** For each hunk, build the pre-change block
   (context + removals) and search for an exact occurrence in the file.
   If there's exactly one, replace it with the post-change block.
2. **Whitespace-fuzzy fallback.** If exact fails or is ambiguous, retry
   with leading/trailing whitespace normalised per line. Requires a
   unique match — ambiguous hits still fail the hunk.

Every hunk's outcome is recorded in `PatchResult.hunks`, so the model
can see exactly what failed and where. Failed hunks don't poison the
file: we only write the buffer back if `all_hunks_applied` **or** the
caller set `partial=True` (not exposed to the LLM; reserved for the
validator-loop's retry path).

Patch format we accept — a loose unified diff without the `---` /
`+++` file headers (the tool already knows the target path):

    @@ -10,5 +10,6 @@
     context line
    -removed line
    +added line 1
    +added line 2
     context line
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai import ModelRetry, RunContext

from app.agents.coder.context import CoderDeps
from app.agents.coder.errors import PatchApplyError, ToolInputError
from app.agents.coder.results import PatchHunkResult, PatchResult
from app.agents.coder.tools._paths import rel_to, resolve_inside

if TYPE_CHECKING:
    from pydantic_ai import Agent


# Shown to the model when its patch can't even be parsed. Kept terse —
# the `apply_patch` tool docstring already carries the format spec; the
# nudge here is just to point at *this* failure mode (empty/headerless
# patch) so the model doesn't retry the same broken shape.
_MALFORMED_PATCH_HINT = (
    "Your patch must contain at least one hunk starting with "
    "`@@ -old_start,old_len +new_start,new_len @@`. For a brand-new "
    "file call write_file instead. For an edit, include the hunk "
    "header followed by context lines (single-space prefix), removals "
    "(`-`), and additions (`+`)."
)


# Cap on the file excerpt we embed in apply_patch retry messages.
# Big enough to anchor most edits (the agent only needs ~40 lines of
# verbatim context to repatch), small enough that we don't blow the
# token budget on a 4K-line scaffold file. If the file is larger than
# this we tell the model so it knows to call read_file for the rest.
_RETRY_EXCERPT_MAX_BYTES = 8000


def _read_file_excerpt(workspace_root: Path, path: str) -> str | None:
    """Return the first ~8KB of `path` for inclusion in apply_patch errors.

    None if the file does not exist or cannot be read — caller suppresses
    the embed in that case (the existing "use write_file" hint already
    covers the missing-file branch). On read errors we swallow the
    exception: a degraded retry message is strictly better than a
    crashed apply_patch tool, and the agent will fall back to read_file
    on the next turn.
    """
    try:
        abs_path = resolve_inside(workspace_root, path)
    except Exception:  # noqa: BLE001 — path resolution is best-effort here
        return None
    if not abs_path.exists() or not abs_path.is_file():
        return None
    try:
        text = abs_path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — see docstring
        return None
    if len(text) <= _RETRY_EXCERPT_MAX_BYTES:
        return text
    truncated = text[:_RETRY_EXCERPT_MAX_BYTES]
    return (
        f"{truncated}\n"
        f"... (file truncated at {_RETRY_EXCERPT_MAX_BYTES} bytes; "
        f"call `read_file` for the rest)"
    )


_HUNK_HEADER_RE = re.compile(
    r"^@@\s+-(?P<old_start>\d+)(?:,(?P<old_len>\d+))?\s+\+(?P<new_start>\d+)(?:,(?P<new_len>\d+))?\s+@@",
)


@dataclass
class _Hunk:
    """One hunk of a unified diff, pre-parsed into before/after line lists."""

    old_start: int  # 1-indexed from the patch header (advisory; we don't trust it)
    before: list[str]  # context + removals, in original order
    after: list[str]  # context + additions, in original order


def _parse_patch(patch_text: str) -> list[_Hunk]:
    """Parse a unified-diff blob into hunks. Raises `ToolInputError` on
    malformed input — ambiguous diffs are the model's problem, not the
    applier's.
    """
    if not patch_text.strip():
        raise ToolInputError("patch is empty")

    lines = patch_text.splitlines()
    hunks: list[_Hunk] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Skip file headers if the model includes them anyway.
        if line.startswith(("--- ", "+++ ", "diff ", "index ")):
            i += 1
            continue
        if not line:
            i += 1
            continue
        m = _HUNK_HEADER_RE.match(line)
        if not m:
            # Drift before the first hunk — tolerate it (commentary).
            # After we've seen a hunk, a bare line is malformed.
            if hunks:
                raise ToolInputError(
                    f"unexpected line outside hunk: {line!r}"
                )
            i += 1
            continue

        old_start = int(m.group("old_start"))
        before: list[str] = []
        after: list[str] = []
        i += 1
        while i < len(lines):
            line = lines[i]
            if not line:
                # Blank line inside a hunk is a context-of-blank; diff
                # format represents it as " " (single space) but LLMs
                # sometimes drop the leading space. Treat empty as
                # context too.
                before.append("")
                after.append("")
                i += 1
                continue
            marker, rest = line[0], line[1:]
            if marker == " ":
                before.append(rest)
                after.append(rest)
                i += 1
            elif marker == "-":
                before.append(rest)
                i += 1
            elif marker == "+":
                after.append(rest)
                i += 1
            elif marker == "\\":
                # "\ No newline at end of file" — metadata, skip.
                i += 1
            elif marker == "@":
                # Next hunk starts; bail the inner loop.
                break
            else:
                # Trailing commentary / blank-at-end-of-diff; stop the
                # hunk here.
                break
        hunks.append(_Hunk(old_start=old_start, before=before, after=after))

    if not hunks:
        raise ToolInputError("no hunks found in patch")
    return hunks


def _normalise(s: str) -> str:
    """Collapse leading+trailing whitespace for fuzzy matching.

    We keep the *content* intact — only leading indentation and trailing
    spaces get normalised. This is what rescues LLM diffs that lose a
    level of indent after Black reformats a file.
    """
    return s.strip()


def _find_exact(lines: list[str], before: list[str]) -> int:
    """Return the 0-indexed start where `before` matches `lines` exactly.

    Returns `-1` if no match, `-2` if multiple matches (ambiguous).
    """
    if not before:
        # Empty "before" means a pure insertion with no anchor — the
        # caller can still apply it at the header-advertised line, but
        # that path is handled upstream; here we signal "no match".
        return -1
    n, m = len(lines), len(before)
    first = -1
    i = 0
    while i <= n - m:
        if lines[i : i + m] == before:
            if first == -1:
                first = i
                i += 1
            else:
                return -2  # ambiguous
        else:
            i += 1
    return first


def _find_fuzzy(lines: list[str], before: list[str]) -> int:
    """Whitespace-insensitive counterpart of `_find_exact`."""
    if not before:
        return -1
    norm_before = [_normalise(s) for s in before]
    norm_lines = [_normalise(s) for s in lines]
    n, m = len(lines), len(before)
    first = -1
    i = 0
    while i <= n - m:
        if norm_lines[i : i + m] == norm_before:
            if first == -1:
                first = i
                i += 1
            else:
                return -2
        else:
            i += 1
    return first


def _apply_patch_to_text(text: str, patch_text: str) -> tuple[str, list[PatchHunkResult]]:
    """Pure function: (original text, patch text) → (new text, per-hunk results).

    Separated from the tool so it's unit-testable without a workspace.
    On any hunk failure, we still return the partially applied text
    (hunks applied so far). The tool handler decides whether to write.
    """
    hunks = _parse_patch(patch_text)

    # Work line-by-line, preserving original newlines. We re-join with
    # the *dominant* line-ending at the end.
    lines = text.splitlines()
    # Detect trailing newline so we can preserve it.
    had_trailing_nl = text.endswith("\n")

    results: list[PatchHunkResult] = []

    for idx, hunk in enumerate(hunks):
        # 1) Exact match anywhere in the file.
        match = _find_exact(lines, hunk.before)
        method: str | None = "exact"
        if match < 0:
            # 2) Fuzzy match — whitespace normalised.
            match = _find_fuzzy(lines, hunk.before)
            method = "fuzzy"

        if match == -2:
            results.append(
                PatchHunkResult(
                    hunk_index=idx,
                    applied=False,
                    reason=f"ambiguous match — before-block occurs multiple times ({method})",
                )
            )
            continue
        if match < 0:
            # Special case: no `before` block (pure insertion). Fall
            # back to inserting at the header-advertised line (1-indexed).
            if not hunk.before:
                insert_at = max(0, min(len(lines), hunk.old_start - 1))
                lines[insert_at:insert_at] = hunk.after
                results.append(
                    PatchHunkResult(
                        hunk_index=idx,
                        applied=True,
                        matched_line=insert_at + 1,
                        reason="pure-insert at header line",
                    )
                )
                continue
            results.append(
                PatchHunkResult(
                    hunk_index=idx,
                    applied=False,
                    reason="no matching context found in file",
                )
            )
            continue

        # Replace [match : match + len(before)] with `after`.
        lines[match : match + len(hunk.before)] = hunk.after
        results.append(
            PatchHunkResult(
                hunk_index=idx,
                applied=True,
                matched_line=match + 1,
                reason=None if method == "exact" else "whitespace-fuzzy match",
            )
        )

    new_text = "\n".join(lines)
    if had_trailing_nl:
        new_text += "\n"
    return new_text, results


def _apply_patch(root: Path, rel: str, patch_text: str) -> PatchResult:
    if not rel or not rel.strip():
        raise ToolInputError("path must not be empty")
    path = resolve_inside(root, rel)
    if not path.exists():
        raise ToolInputError(
            f"cannot patch file that does not exist: {rel!r}; use write_file to create it"
        )
    if path.is_dir():
        raise ToolInputError(f"cannot patch a directory: {rel!r}")

    original = path.read_text(encoding="utf-8", errors="replace")
    new_text, hunk_results = _apply_patch_to_text(original, patch_text)

    applied_count = sum(1 for r in hunk_results if r.applied)
    ok = applied_count == len(hunk_results)

    if ok:
        data = new_text.encode("utf-8")
        path.write_bytes(data)
        return PatchResult(
            path=rel_to(root, path),
            ok=True,
            hunks_applied=applied_count,
            hunks_total=len(hunk_results),
            hunks=hunk_results,
            bytes_written=len(data),
        )

    # One or more hunks failed — don't write partial state. Surface the
    # per-hunk detail via `PatchApplyError` so pydantic-ai forwards the
    # structured error to the model as a retry hint.
    raise PatchApplyError(
        f"apply_patch: {applied_count}/{len(hunk_results)} hunks applied to {rel}",
        details=[r.model_dump() for r in hunk_results],
    )


def register(agent: Agent[CoderDeps, str]) -> None:
    """Attach `apply_patch` to `agent`."""

    @agent.tool
    async def apply_patch(
        ctx: RunContext[CoderDeps],
        path: str,
        patch: str,
    ) -> PatchResult:
        """Apply a unified-diff-style `patch` to an existing file at `path`.

        The patch may omit the `--- old` / `+++ new` header lines since
        the target file is specified explicitly. Each hunk starts with
        `@@ -a,b +c,d @@`; lines prefixed with a single space are
        context, `-` is removed, `+` is added.

        On a clean apply the file is overwritten and a `PatchResult` with
        `ok=True` is returned. On any hunk failure nothing is written
        and the model receives a structured error with per-hunk
        diagnostics (which hunks missed, and why) so it can retry.
        """
        ctx.deps.bind(tool="apply_patch", path=path, patch_bytes=len(patch)).debug("coder.tool")
        try:
            result = _apply_patch(ctx.deps.workspace_root, path, patch)
            # Record the edit so the validator loop can scope lint/type
            # checks to files actually touched this turn.
            ctx.deps.touched_paths.add(result.path)
            return result
        except PatchApplyError as exc:
            # Translate into pydantic-ai's retry-hint exception so the
            # model sees the diagnostics as a tool-return, not as an
            # uncaught error that blows up the run. Direct callers of
            # `_apply_patch` (tests, the validator-loop retry path) keep
            # seeing the structured `PatchApplyError` with `.details`.
            #
            # Include the actual file contents (truncated) inline. Phase-1
            # traces showed the agent looping through three apply_patch
            # ModelRetries — each one with the same stale context — and
            # exhausting pydantic-ai's per-tool budget. Telling the model
            # "go re-read with read_file" in prose was insufficient:
            # under low reasoning effort it would re-emit a near-identical
            # patch instead of issuing the read_file call. Embedding the
            # file slice directly in the retry payload eliminates that
            # branch — the model always has fresh anchors to copy from.
            detail_lines = [f"- {d}" for d in (exc.details or [])]
            file_excerpt = _read_file_excerpt(ctx.deps.workspace_root, path)
            raise ModelRetry(
                "apply_patch failed: "
                + str(exc)
                + ("\n" + "\n".join(detail_lines) if detail_lines else "")
                + (f"\n\nCurrent file contents of {path}:\n{file_excerpt}" if file_excerpt else "")
                + "\nEmit a new patch whose @@ context lines match the "
                "above byte-for-byte, or use write_file if the file does "
                "not yet exist."
            ) from exc
        except ToolInputError as exc:
            # Malformed patch (empty, no hunks, bogus header, …).
            # Surfacing this as ModelRetry lets the model correct its
            # next emission on the same turn instead of crashing the
            # whole agent run and landing us in the outer loop's
            # agent-error retry path (which would lose the file context
            # the model just built up).
            raise ModelRetry(
                f"apply_patch: {exc}\n{_MALFORMED_PATCH_HINT}"
            ) from exc
