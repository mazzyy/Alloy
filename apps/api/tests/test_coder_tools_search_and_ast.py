"""Unit tests for `search_code` and `ast_summary`.

We don't rely on `ripgrep` being installed — the module falls back to
a pure-Python walker, which is what CI exercises. When `rg` is
available locally the ripgrep path just gets tested incidentally, and
the assertions (same hits, same shape) still hold.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.coder.errors import ToolInputError
from app.agents.coder.tools.search import _ast_summary, _search_code


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "apps/api/app").mkdir(parents=True)
    (tmp_path / "apps/web/src").mkdir(parents=True)
    (tmp_path / "node_modules").mkdir()

    (tmp_path / "apps/api/app/models.py").write_text(
        "from pydantic import BaseModel\n"
        "from sqlmodel import SQLModel\n"
        "\n"
        "class User(SQLModel):\n"
        "    id: int\n"
        "\n"
        "class Foo(BaseModel):\n"
        "    name: str\n"
        "\n"
        "async def get_user(user_id: int):\n"
        "    return None\n"
        "\n"
        "def _helper(x):\n"
        "    return x\n",
        encoding="utf-8",
    )
    (tmp_path / "apps/web/src/App.tsx").write_text(
        "export const foo = 1;\n"
        "export function bar() { return 2 }\n"
        "export default class App {}\n",
        encoding="utf-8",
    )
    (tmp_path / "node_modules/noise.js").write_text("export const ignored = 1;\n", encoding="utf-8")
    return tmp_path


# ── search_code ────────────────────────────────────────────────────────


async def test_search_code_finds_literal_in_python_file(workspace: Path) -> None:
    hits = await _search_code(workspace, "class User", k=20)
    paths = [h.path for h in hits.hits]
    assert "apps/api/app/models.py" in paths


async def test_search_code_skips_noise_directories(workspace: Path) -> None:
    # `ignored = 1` only appears in node_modules; must not surface.
    hits = await _search_code(workspace, "ignored = 1", k=20)
    assert all("node_modules" not in h.path for h in hits.hits)


async def test_search_code_rejects_empty_query(workspace: Path) -> None:
    with pytest.raises(ToolInputError, match="non-empty"):
        await _search_code(workspace, "", k=20)


async def test_search_code_rejects_out_of_range_k(workspace: Path) -> None:
    with pytest.raises(ToolInputError, match="between"):
        await _search_code(workspace, "x", k=0)
    with pytest.raises(ToolInputError, match="between"):
        await _search_code(workspace, "x", k=1_000)


# ── ast_summary (Python) ───────────────────────────────────────────────


def test_ast_summary_python_distinguishes_pydantic_models(workspace: Path) -> None:
    summary = _ast_summary(workspace, "apps/api/app/models.py")
    assert summary.language == "python"
    by_name = {s.name: s for s in summary.symbols}
    assert by_name["User"].kind == "pydantic_model"  # SQLModel counts as pydantic_model
    assert by_name["Foo"].kind == "pydantic_model"
    assert by_name["get_user"].kind == "async_function"
    assert by_name["_helper"].kind == "function"


def test_ast_summary_python_captures_imports(workspace: Path) -> None:
    summary = _ast_summary(workspace, "apps/api/app/models.py")
    kinds = {s.kind for s in summary.symbols}
    assert "import" in kinds


def test_ast_summary_python_tolerates_syntax_error(workspace: Path) -> None:
    broken = workspace / "apps/api/app/broken.py"
    broken.write_text("def oops(\n", encoding="utf-8")
    summary = _ast_summary(workspace, "apps/api/app/broken.py")
    assert summary.symbols
    assert summary.symbols[0].kind == "parse_error"


# ── ast_summary (TypeScript) ───────────────────────────────────────────


def test_ast_summary_typescript_captures_exports(workspace: Path) -> None:
    summary = _ast_summary(workspace, "apps/web/src/App.tsx")
    assert summary.language == "typescript"
    names = {s.name for s in summary.symbols}
    assert {"foo", "bar", "App"}.issubset(names)


def test_ast_summary_rejects_unsupported_extension(workspace: Path) -> None:
    other = workspace / "notes.md"
    other.write_text("# nope\n", encoding="utf-8")
    with pytest.raises(ToolInputError, match="only supports"):
        _ast_summary(workspace, "notes.md")
