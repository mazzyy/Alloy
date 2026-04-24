"""`search_code` + `ast_summary`.

Phase 1 scope:

* `search_code` — literal-string / regex search across the workspace
  using `ripgrep` if it's on PATH, falling back to a pure-Python
  walker so CI doesn't need `rg` installed. Results capped at 100
  hits and returned as `SearchHits`.
* `ast_summary` — Python-only for now. We parse with the stdlib `ast`
  module and emit classes, functions, Pydantic models, and top-level
  imports. TypeScript summaries land in Task #23 when tree-sitter
  comes online.

Both tools return *paths relative to the workspace root* — the model
never sees an absolute path.
"""

from __future__ import annotations

import ast
import asyncio
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai import RunContext

from app.agents.coder.context import CoderDeps
from app.agents.coder.errors import ToolInputError
from app.agents.coder.results import AstSummary, AstSymbol, SearchHit, SearchHits
from app.agents.coder.tools._paths import rel_to, resolve_inside

if TYPE_CHECKING:
    from pydantic_ai import Agent


_HIT_CAP = 100

# Directories we don't search — same rationale as files.py's listing skip.
_SEARCH_SKIP = frozenset(
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


async def _search_ripgrep(query: str, root: Path, k: int) -> tuple[list[SearchHit], bool]:
    """Run `rg --json` and parse the streaming output.

    Returns `(hits, truncated)`. If ripgrep isn't available the caller
    falls back to `_search_python`.
    """
    rg = shutil.which("rg")
    if not rg:
        return [], False
    # `--no-messages` silences "ripgrep: unknown filetype" noise.
    # `--max-count k` caps per-file matches; we also cap globally below.
    proc = await asyncio.create_subprocess_exec(
        rg,
        "--json",
        "--no-messages",
        "--line-number",
        "--fixed-strings",
        query,
        str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return [], False

    import json as _json

    hits: list[SearchHit] = []
    for line in out_b.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            obj = _json.loads(line)
        except ValueError:
            continue
        if obj.get("type") != "match":
            continue
        data = obj.get("data", {})
        path_text = data.get("path", {}).get("text") or ""
        line_no = int(data.get("line_number") or 0)
        text = (data.get("lines", {}) or {}).get("text") or ""
        if not path_text or not line_no:
            continue
        try:
            rel = rel_to(root, Path(path_text))
        except Exception:
            continue
        # Skip noise dirs; ripgrep usually respects .gitignore but our
        # `_SEARCH_SKIP` set is stricter.
        if any(part in _SEARCH_SKIP for part in Path(rel).parts):
            continue
        hits.append(SearchHit(path=rel, line=line_no, content=text.rstrip("\n")))
        if len(hits) >= k:
            return hits, True
    return hits, False


def _search_python(query: str, root: Path, k: int) -> tuple[list[SearchHit], bool]:
    """Pure-Python fallback — iterate files, split lines, linear scan."""
    needle = query
    hits: list[SearchHit] = []
    for path in root.rglob("*"):
        if any(part in _SEARCH_SKIP for part in path.relative_to(root).parts):
            continue
        if not path.is_file():
            continue
        # Cheap binary guard.
        try:
            with path.open("rb") as fp:
                head = fp.read(1024)
        except OSError:
            continue
        if b"\x00" in head:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if needle in line:
                hits.append(
                    SearchHit(
                        path=rel_to(root, path),
                        line=i,
                        content=line.rstrip(),
                    )
                )
                if len(hits) >= k:
                    return hits, True
    return hits, False


async def _search_code(root: Path, query: str, k: int) -> SearchHits:
    if not query:
        raise ToolInputError("search query must be non-empty")
    if k < 1 or k > _HIT_CAP:
        raise ToolInputError(f"k must be between 1 and {_HIT_CAP}, got {k}")
    rg_hits, trunc = await _search_ripgrep(query, root, k)
    if rg_hits:
        return SearchHits(query=query, hits=rg_hits, truncated=trunc)
    py_hits, trunc = _search_python(query, root, k)
    return SearchHits(query=query, hits=py_hits, truncated=trunc)


# ── ast_summary ────────────────────────────────────────────────────────


_PYDANTIC_BASES = frozenset({"BaseModel", "SQLModel", "SettingsConfigDict"})


def _python_summary(path: Path) -> AstSummary:
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        # Don't fail the tool — a broken file is exactly the kind of
        # state the agent is trying to read. Surface what we can.
        return AstSummary(
            path=path.name,
            language="python",
            symbols=[
                AstSymbol(
                    kind="parse_error",
                    name=f"SyntaxError: {exc.msg}",
                    line=exc.lineno or 0,
                )
            ],
        )

    symbols: list[AstSymbol] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                symbols.append(
                    AstSymbol(
                        kind="import",
                        name=alias.asname or alias.name,
                        line=node.lineno,
                        signature=f"import {alias.name}",
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = ", ".join(a.asname or a.name for a in node.names)
            symbols.append(
                AstSymbol(
                    kind="import",
                    name=f"{mod}.{names}" if mod else names,
                    line=node.lineno,
                    signature=f"from {mod} import {names}",
                )
            )
        elif isinstance(node, ast.ClassDef):
            base_names = [_unparse(b) for b in node.bases]
            kind = (
                "pydantic_model"
                if any(b.split(".")[-1] in _PYDANTIC_BASES for b in base_names)
                else "class"
            )
            sig = f"class {node.name}({', '.join(base_names)})" if base_names else f"class {node.name}"
            symbols.append(AstSymbol(kind=kind, name=node.name, line=node.lineno, signature=sig))
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            args = ", ".join(a.arg for a in node.args.args)
            sig = f"{'async def' if isinstance(node, ast.AsyncFunctionDef) else 'def'} {node.name}({args})"
            symbols.append(
                AstSymbol(
                    kind="async_function" if isinstance(node, ast.AsyncFunctionDef) else "function",
                    name=node.name,
                    line=node.lineno,
                    signature=sig,
                )
            )
    return AstSummary(path=path.name, language="python", symbols=symbols)


def _unparse(node: ast.AST) -> str:
    """Tight ast.unparse wrapper that never raises on partial trees."""
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001 — best-effort summary, not correctness-critical
        return "<?>"


_TS_EXPORT_RE = re.compile(
    r"^\s*export\s+(?:default\s+)?(?:async\s+)?"
    r"(?:function|class|const|let|var|interface|type|enum)\s+(?P<name>[A-Za-z_$][\w$]*)",
    re.MULTILINE,
)


def _typescript_summary(path: Path) -> AstSummary:
    """Regex-based TS summary — rough but useful until tree-sitter lands."""
    source = path.read_text(encoding="utf-8", errors="replace")
    symbols: list[AstSymbol] = []
    for m in _TS_EXPORT_RE.finditer(source):
        line_no = source.count("\n", 0, m.start()) + 1
        symbols.append(
            AstSymbol(
                kind="export",
                name=m.group("name"),
                line=line_no,
                signature=m.group(0).strip(),
            )
        )
    return AstSummary(path=path.name, language="typescript", symbols=symbols)


def _ast_summary(root: Path, rel: str) -> AstSummary:
    path = resolve_inside(root, rel)
    if not path.exists() or not path.is_file():
        raise ToolInputError(f"file does not exist or is not a file: {rel!r}")

    rel_path = rel_to(root, path)
    suffix = path.suffix.lower()
    if suffix == ".py":
        summary = _python_summary(path)
    elif suffix in {".ts", ".tsx", ".js", ".jsx"}:
        summary = _typescript_summary(path)
    else:
        raise ToolInputError(
            f"ast_summary only supports .py / .ts / .tsx / .js / .jsx, got {suffix!r}"
        )
    # Rebuild with the relative path (the private helpers stamp only the name).
    return AstSummary(path=rel_path, language=summary.language, symbols=summary.symbols)


def register(agent: Agent[CoderDeps, str]) -> None:
    """Attach `search_code` and `ast_summary`."""

    @agent.tool
    async def search_code(
        ctx: RunContext[CoderDeps],
        query: str,
        k: int = 20,
    ) -> SearchHits:
        """Search the workspace for `query` (literal string match) and
        return up to `k` hits as `{path, line, content}`.

        Uses `ripgrep` if installed, otherwise a Python walker. Hits from
        common noise directories (node_modules, .git, .venv, ...) are
        filtered out.
        """
        ctx.deps.bind(tool="search_code", query=query, k=k).debug("coder.tool")
        return await _search_code(ctx.deps.workspace_root, query, k)

    @agent.tool
    async def ast_summary(
        ctx: RunContext[CoderDeps],
        path: str,
    ) -> AstSummary:
        """Return a structural summary of a Python or TypeScript file —
        classes, functions, Pydantic models, top-level imports.

        Much cheaper than reading the whole file when you only need to
        know what's defined where.
        """
        ctx.deps.bind(tool="ast_summary", path=path).debug("coder.tool")
        return _ast_summary(ctx.deps.workspace_root, path)
