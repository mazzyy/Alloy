"""Unit tests for `app.agents.coder.tools.patch`.

We test `_apply_patch_to_text` (pure function, no FS) and `_apply_patch`
(touches a temp dir). The patch format the tool accepts is loose —
LLMs often drop the `--- old / +++ new` header lines and sometimes
get indentation slightly wrong after auto-format. Both are covered.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.coder.errors import PatchApplyError, ToolInputError
from app.agents.coder.tools.patch import _apply_patch, _apply_patch_to_text


# ── Pure-function patch applier ────────────────────────────────────────


def test_apply_patch_to_text_single_hunk_exact() -> None:
    original = "line a\nline b\nline c\n"
    patch = "@@ -1,3 +1,3 @@\n line a\n-line b\n+line B\n line c\n"
    new, results = _apply_patch_to_text(original, patch)
    assert new == "line a\nline B\nline c\n"
    assert len(results) == 1
    assert results[0].applied is True
    assert results[0].matched_line == 1


def test_apply_patch_to_text_multiple_hunks() -> None:
    original = "\n".join(f"line {i}" for i in range(1, 11)) + "\n"
    patch = (
        "@@ -1,3 +1,3 @@\n line 1\n-line 2\n+LINE 2\n line 3\n"
        "@@ -8,3 +8,3 @@\n line 8\n-line 9\n+LINE 9\n line 10\n"
    )
    new, results = _apply_patch_to_text(original, patch)
    assert "LINE 2" in new
    assert "LINE 9" in new
    assert all(r.applied for r in results)
    assert len(results) == 2


def test_apply_patch_to_text_fuzzy_indent_drift() -> None:
    """LLMs often drop a level of indent after Black reformats. Fuzzy
    matching (whitespace-insensitive) should rescue this."""
    original = "def foo():\n    x = 1\n    y = 2\n"
    # Agent emits the patch with *no* leading indent on context lines.
    patch = "@@ -1,3 +1,3 @@\n def foo():\n-    x = 1\n+    x = 42\n     y = 2\n"
    new, results = _apply_patch_to_text(original, patch)
    assert new == "def foo():\n    x = 42\n    y = 2\n"
    assert results[0].applied


def test_apply_patch_to_text_ambiguous_match_fails_hunk() -> None:
    """A pre-change block that occurs twice should fail rather than
    guess which occurrence the agent meant."""
    original = "x = 1\ny = 2\nx = 1\n"
    patch = "@@ -1,1 +1,1 @@\n-x = 1\n+x = 2\n"
    new, results = _apply_patch_to_text(original, patch)
    # Ambiguous: two occurrences of "x = 1". Hunk should NOT apply.
    assert new == original
    assert results[0].applied is False
    assert "ambiguous" in (results[0].reason or "")


def test_apply_patch_to_text_no_match_fails_hunk() -> None:
    original = "just one line\n"
    patch = "@@ -1,1 +1,1 @@\n-nope nope nope\n+replacement\n"
    new, results = _apply_patch_to_text(original, patch)
    assert new == original
    assert results[0].applied is False
    assert "no matching context" in (results[0].reason or "")


def test_apply_patch_to_text_pure_insert_uses_header_line() -> None:
    """Hunk with only `+` lines (no `-`, no ` ` context) inserts at
    the header-advertised line."""
    original = "a\nb\nc\n"
    patch = "@@ -2,0 +2,1 @@\n+inserted\n"
    new, results = _apply_patch_to_text(original, patch)
    assert new == "a\ninserted\nb\nc\n"
    assert results[0].applied


def test_apply_patch_to_text_preserves_trailing_newline() -> None:
    # Original has trailing newline — result must too.
    original = "a\nb\n"
    patch = "@@ -1,2 +1,2 @@\n-a\n+A\n b\n"
    new, _ = _apply_patch_to_text(original, patch)
    assert new == "A\nb\n"
    # Original without trailing newline — result must match.
    original2 = "a\nb"
    new2, _ = _apply_patch_to_text(original2, patch)
    assert new2 == "A\nb"


def test_apply_patch_to_text_rejects_empty_patch() -> None:
    with pytest.raises(ToolInputError, match="empty"):
        _apply_patch_to_text("anything\n", "")


def test_apply_patch_to_text_rejects_hunkless_patch() -> None:
    with pytest.raises(ToolInputError, match="no hunks"):
        _apply_patch_to_text("anything\n", "--- old\n+++ new\n")


# ── FS-level tool wrapper ──────────────────────────────────────────────


def test_apply_patch_writes_on_success(tmp_path: Path) -> None:
    target = tmp_path / "file.py"
    target.write_text("a\nb\nc\n", encoding="utf-8")
    patch = "@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
    result = _apply_patch(tmp_path, "file.py", patch)
    assert result.ok is True
    assert target.read_text() == "a\nB\nc\n"
    assert result.bytes_written == len(b"a\nB\nc\n")


def test_apply_patch_raises_on_hunk_failure_and_leaves_file_untouched(
    tmp_path: Path,
) -> None:
    target = tmp_path / "file.py"
    target.write_text("a\nb\nc\n", encoding="utf-8")
    bad_patch = "@@ -1,1 +1,1 @@\n-nothing like this\n+replacement\n"
    with pytest.raises(PatchApplyError) as exc_info:
        _apply_patch(tmp_path, "file.py", bad_patch)
    # File must not have been mutated on failure.
    assert target.read_text() == "a\nb\nc\n"
    # The error carries per-hunk detail for the LLM to act on.
    assert exc_info.value.details
    assert exc_info.value.details[0]["applied"] is False


def test_apply_patch_refuses_nonexistent_file(tmp_path: Path) -> None:
    with pytest.raises(ToolInputError, match="does not exist"):
        _apply_patch(tmp_path, "no_such_file.py", "@@ -1,1 +1,1 @@\n-x\n+y\n")


def test_apply_patch_rejects_empty_path(tmp_path: Path) -> None:
    """Regression: the model emitted `apply_patch(path="", patch=...)`
    in an Azure run, which previously fell through to `resolve_inside`
    and surfaced a confusing error. Now rejected early with a clear
    ToolInputError; the tool-handler translates it to ModelRetry so the
    same turn can correct the path."""
    with pytest.raises(ToolInputError, match="path must not be empty"):
        _apply_patch(tmp_path, "", "@@ -1,1 +1,1 @@\n-x\n+y\n")
    with pytest.raises(ToolInputError, match="path must not be empty"):
        _apply_patch(tmp_path, "   ", "@@ -1,1 +1,1 @@\n-x\n+y\n")


# ── Agent-level tool handler (`apply_patch` registered on an Agent) ────
#
# These tests guard a real regression we hit: the model emitted a patch
# the parser couldn't read ("no hunks found in patch"), which escaped the
# tool handler as `ToolInputError`, crashed the whole `agent.run()`, and
# landed in the validator loop's *crash* path — consuming an attempt and
# serving the model a generic nudge instead of a targeted retry hint.
# The fix was to translate `ToolInputError` from `apply_patch` into
# `ModelRetry` so the agent can self-correct on the same turn.
#
# We exercise the handler by building a minimal Agent with a FunctionModel
# that calls `apply_patch` once with a malformed patch, then — after
# seeing the retry hint — emits a final text message. Production success
# criteria: `agent.run()` returns cleanly and the retry hint was
# delivered through the normal tool-return channel, not as an exception.

import pytest as _pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.agents.coder.context import CoderDeps
from app.agents.coder.tools.patch import register as register_patch_tool


pytestmark_agent = _pytest.mark.asyncio


@_pytest.mark.asyncio
async def test_apply_patch_handler_converts_hunkless_patch_to_model_retry(
    tmp_path: Path,
) -> None:
    """A malformed (hunkless) patch must NOT crash `agent.run()` — it
    must reach the model as a retry hint so the next turn can fix it."""
    target = tmp_path / "file.py"
    target.write_text("a\nb\nc\n", encoding="utf-8")

    call_count = {"n": 0}
    saw_retry_hint = {"seen": False}

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First turn — fire off a malformed patch so the handler has
            # a reason to raise ModelRetry internally.
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="apply_patch",
                        args={"path": "file.py", "patch": "--- old\n+++ new\n"},
                        tool_call_id="call-1",
                    )
                ]
            )
        # Second turn — pydantic-ai has forwarded the ModelRetry text as
        # a tool-retry message. Confirm we saw the hint, then close out.
        for m in messages:
            for p in getattr(m, "parts", []):
                content = getattr(p, "content", "") or ""
                if "apply_patch" in str(content) and "hunk" in str(content):
                    saw_retry_hint["seen"] = True
        return ModelResponse(parts=[TextPart(content="ok, will retry with a real hunk")])

    agent = Agent[CoderDeps, str](
        model=FunctionModel(respond),
        deps_type=CoderDeps,
        output_type=str,
        system_prompt="t",
        retries=2,  # needs to be >=1 so ModelRetry actually loops
    )
    register_patch_tool(agent)
    deps = CoderDeps(workspace_root=tmp_path)

    result = await agent.run("edit file.py", deps=deps)

    assert result.output == "ok, will retry with a real hunk"
    assert call_count["n"] == 2, (
        f"expected exactly one retry turn after the malformed patch, "
        f"got {call_count['n']}"
    )
    assert saw_retry_hint["seen"], (
        "model never saw the 'hunk' retry hint — the handler probably "
        "let the ToolInputError escape as an uncaught exception again"
    )
    # File must not have been mutated by the bad patch.
    assert target.read_text() == "a\nb\nc\n"


# 8th-regression follow-up — when `apply_patch` fails on context mismatch
# (not malformed input), the retry payload must include the actual file
# contents inline so the agent has fresh anchors without needing to call
# `read_file` first. Phase-1 traces showed the agent re-emitting the same
# stale-context patch three times in a row, exhausting pydantic-ai's
# per-tool retry budget and crashing the whole turn with
# `UnexpectedModelBehavior`. Embedding the file slice in the ModelRetry
# message eliminates that loop.


@_pytest.mark.asyncio
async def test_apply_patch_context_miss_embeds_file_excerpt(tmp_path: Path) -> None:
    target = tmp_path / "models.py"
    target.write_text(
        "class User(SQLModel, table=True):\n"
        "    id: int\n"
        "    email: str\n",
        encoding="utf-8",
    )

    seen_excerpt = {"value": False}

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        # Inspect every prior message for the embedded file contents.
        for m in messages:
            for p in getattr(m, "parts", []):
                content = str(getattr(p, "content", "") or "")
                # The retry payload must contain the literal file body.
                if "Current file contents of models.py" in content and "class User" in content:
                    seen_excerpt["value"] = True
        # First turn: emit a patch whose context doesn't match (the file
        # has `class User`, but the patch claims to find `class Order`).
        if not seen_excerpt["value"]:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="apply_patch",
                        args={
                            "path": "models.py",
                            "patch": (
                                "@@ -1,3 +1,4 @@\n"
                                " class Order(SQLModel, table=True):\n"
                                "     id: int\n"
                                "+    total: float\n"
                                "     email: str\n"
                            ),
                        },
                        tool_call_id="call-1",
                    )
                ]
            )
        # Second turn: confirm we saw the excerpt; close out.
        return ModelResponse(parts=[TextPart(content="saw file excerpt")])

    agent = Agent[CoderDeps, str](
        model=FunctionModel(respond),
        deps_type=CoderDeps,
        output_type=str,
        system_prompt="t",
        retries=2,
    )
    register_patch_tool(agent)
    deps = CoderDeps(workspace_root=tmp_path)

    result = await agent.run("edit models.py", deps=deps)

    assert result.output == "saw file excerpt"
    assert seen_excerpt["value"], (
        "context-miss retry payload did not include the file contents "
        "inline — agent will keep re-emitting stale-context patches"
    )
    # File must not have been mutated by the failed patch.
    assert target.read_text().startswith("class User")
