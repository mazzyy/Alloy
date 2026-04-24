"""Unit tests for `app.agents.coder.tools.files`.

We exercise the private helpers (`_list_dir`, `_read_file`, `_write_file`)
directly rather than going through the Pydantic AI agent — no LLM, no
`RunContext`, just deterministic filesystem state.

The agent-registration path is covered in `test_coder_agent.py` via a
`FunctionModel` that scripts a tool-call sequence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.coder.errors import ToolInputError, WorkspaceEscapeError
from app.agents.coder.tools.files import _list_dir, _read_file, _write_file


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A mini scaffold: a couple of files in a couple of directories."""
    (tmp_path / "apps/api/app").mkdir(parents=True)
    (tmp_path / "apps/web/src").mkdir(parents=True)
    (tmp_path / "apps/api/app/main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n",
        encoding="utf-8",
    )
    (tmp_path / "apps/web/src/App.tsx").write_text(
        "export default function App() { return <div />; }\n",
        encoding="utf-8",
    )
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules/ignored.js").write_text("// noise\n", encoding="utf-8")
    return tmp_path


# ── list_files ─────────────────────────────────────────────────────────


def test_list_files_skips_noise_directories(workspace: Path) -> None:
    result = _list_dir(workspace, ".", pattern=None)
    names = {e.path for e in result.entries}
    # Top-level "apps" dir listed; "node_modules" filtered by _LIST_SKIP.
    assert "apps" in names
    assert "node_modules" not in names


def test_list_files_pattern_recursive(workspace: Path) -> None:
    result = _list_dir(workspace, ".", pattern="**/*.py")
    py_paths = [e.path for e in result.entries]
    assert "apps/api/app/main.py" in py_paths
    # Recursive walk MUST honour _LIST_SKIP — no matches from node_modules,
    # .git, .venv etc. The fixture only seeds node_modules but the skip
    # set is broader; the invariant we assert here is the concrete one.
    assert not any("node_modules" in p for p in py_paths)


def test_list_files_directory_must_exist(workspace: Path) -> None:
    with pytest.raises(ToolInputError, match="does not exist"):
        _list_dir(workspace, "does/not/exist", pattern=None)


def test_list_files_rejects_parent_escape(workspace: Path) -> None:
    with pytest.raises(WorkspaceEscapeError):
        _list_dir(workspace, "../../etc", pattern=None)


# ── read_file ──────────────────────────────────────────────────────────


def test_read_file_returns_full_content(workspace: Path) -> None:
    result = _read_file(workspace, "apps/api/app/main.py", None, None)
    assert result.start_line == 1
    assert result.line_count == 2
    assert "FastAPI" in result.content
    assert result.clipped is False


def test_read_file_honours_line_range(workspace: Path) -> None:
    # Seed a larger file so a range is meaningful.
    target = workspace / "big.py"
    target.write_text("\n".join(f"# line {i}" for i in range(1, 101)) + "\n", encoding="utf-8")
    result = _read_file(workspace, "big.py", start_line=10, end_line=12)
    assert result.start_line == 10
    assert result.end_line == 12
    assert result.content.count("\n") == 3
    assert "# line 10" in result.content
    assert "# line 12" in result.content
    assert "# line 13" not in result.content


def test_read_file_clips_at_cap(workspace: Path) -> None:
    # 3 000 lines > _READ_LINE_CAP (2 000)
    target = workspace / "huge.py"
    target.write_text("\n".join("x" for _ in range(3_000)) + "\n", encoding="utf-8")
    result = _read_file(workspace, "huge.py", start_line=1, end_line=3_000)
    assert result.clipped is True
    assert result.end_line == 2_000


def test_read_file_rejects_inverted_range(workspace: Path) -> None:
    with pytest.raises(ToolInputError, match=">= start_line"):
        _read_file(workspace, "apps/api/app/main.py", start_line=5, end_line=2)


def test_read_file_refuses_binary(workspace: Path) -> None:
    target = workspace / "blob.bin"
    target.write_bytes(b"\x00\x01\x02\x03")
    with pytest.raises(ToolInputError, match="binary"):
        _read_file(workspace, "blob.bin", None, None)


# ── write_file ─────────────────────────────────────────────────────────


def test_write_file_creates_new_file(workspace: Path) -> None:
    result = _write_file(workspace, "apps/api/app/new_module.py", "x = 1\n")
    assert result.created is True
    assert (workspace / "apps/api/app/new_module.py").read_text() == "x = 1\n"


def test_write_file_creates_parent_dirs(workspace: Path) -> None:
    _write_file(workspace, "apps/api/app/sub/dir/file.py", "pass\n")
    assert (workspace / "apps/api/app/sub/dir/file.py").exists()


def test_write_file_refuses_overwrite(workspace: Path) -> None:
    with pytest.raises(ToolInputError, match="apply_patch"):
        _write_file(workspace, "apps/api/app/main.py", "# rewritten\n")


def test_write_file_rejects_empty_path(workspace: Path) -> None:
    """Regression: the model sometimes emits `path=""` when its
    argument extraction slips. Used to fall into `resolve_inside` with
    a confusing error; we now reject early with a clear ToolInputError
    so the retry hint is obvious."""
    with pytest.raises(ToolInputError, match="path must not be empty"):
        _write_file(workspace, "", "x = 1\n")
    with pytest.raises(ToolInputError, match="path must not be empty"):
        _write_file(workspace, "   ", "x = 1\n")


def test_write_file_blocks_workspace_escape(workspace: Path) -> None:
    with pytest.raises(WorkspaceEscapeError):
        _write_file(workspace, "../escape.py", "pwned\n")
