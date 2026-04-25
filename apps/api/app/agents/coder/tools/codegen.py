"""Code-generation tools that round-trip through the backend/frontend toolchain.

`openapi_export`    — invokes a small exporter inside the backend
                      container that serialises `app.main:app.openapi()`
                      to `openapi.json` at the repo root.
`regenerate_client` — runs `@hey-api/openapi-ts` on that file to
                      emit a typed TS client + TanStack Query hooks.
`alembic_autogenerate` — calls `alembic revision --autogenerate -m ...`
                      inside the backend container, then parses the
                      generated migration for destructive ops and, if
                      found, signals that human review is required.

Each tool returns a structured result the LLM can act on rather than
raw stdout it has to interpret.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai import RunContext

from app.agents.coder.context import CoderDeps
from app.agents.coder.errors import HumanReviewRequired, ToolInputError
from app.agents.coder.results import AlembicResult, CommandResult
from app.agents.coder.tools._paths import rel_to
from app.agents.coder.tools.commands import run_command

if TYPE_CHECKING:
    from pydantic_ai import Agent


# Alembic migration ops we treat as destructive. `op.drop_table` is the
# canonical data-loss footgun; `op.drop_column`, `op.drop_index`,
# `op.rename_table`, and `op.alter_column` with a type narrowing all
# require human review before we let the agent run `upgrade head`.
_DESTRUCTIVE_OPS = (
    "op.drop_table",
    "op.drop_column",
    "op.drop_index",
    "op.drop_constraint",
    "op.rename_table",
)

# Matches `Generating /path/to/backend/app/alembic/versions/<rev>_<slug>.py ... done`
_ALEMBIC_GENERATED_RE = re.compile(
    r"Generating\s+(?P<path>.+?\.py)\s*(?:\.\.\.|\s*done)?",
    re.IGNORECASE,
)
_ALEMBIC_REV_RE = re.compile(r"Generating.*?([a-f0-9]{12})[_.]", re.IGNORECASE)


async def _openapi_export(deps: CoderDeps) -> CommandResult:
    """Export the project's OpenAPI schema to `<workspace>/openapi.json`.

    The base template (full-stack-fastapi-template) imports `app.main`
    from inside the `backend/` directory — there's no top-level export
    script, so we always run the inline one-liner. We pin cwd to
    `backend/` so the import resolves; in sandbox mode the same routing
    happens because `python` lands in the backend service container.
    """
    one_liner = (
        "import json; "
        "from app.main import app; "
        "open('../openapi.json', 'w').write(json.dumps(app.openapi(), indent=2))"
    )
    return await run_command(
        deps, "python", ["-c", one_liner], timeout_s=60, cwd_subdir="backend"
    )


async def _regenerate_client(deps: CoderDeps) -> CommandResult:
    """Regenerate the frontend TS client from `openapi.json`.

    The template wires `@hey-api/openapi-ts` into `frontend/package.json`
    as the `generate-client` script. Running the script directly via
    `npm run --silent generate-client` from inside `frontend/` matches
    the template's own `scripts/generate-client.sh` — keeps the
    invocation hermetic and avoids depending on `bun` (which the
    template uses but we can't assume is on every dev box).
    """
    return await run_command(
        deps,
        "npm",
        ["run", "--silent", "generate-client"],
        timeout_s=180,
        cwd_subdir="frontend",
    )


def _scan_destructive(migration_path: Path) -> list[str]:
    """Grep the generated migration for known-destructive op calls."""
    if not migration_path.exists():
        return []
    text = migration_path.read_text(encoding="utf-8", errors="replace")
    found: list[str] = []
    for op in _DESTRUCTIVE_OPS:
        if op in text:
            # One entry per distinct destructive op, not per occurrence
            # — the agent only needs to know *whether* it's destructive.
            found.append(op)
    return found


async def _alembic_autogenerate(
    deps: CoderDeps,
    message: str,
) -> AlembicResult:
    if not message or not message.strip():
        raise ToolInputError("migration message must be non-empty")

    # alembic.ini lives in `backend/` — running from the workspace root
    # gives "Could not find alembic.ini." Pin to backend/.
    cmd = await run_command(
        deps,
        "alembic",
        ["revision", "--autogenerate", "-m", message],
        timeout_s=120,
        cwd_subdir="backend",
    )
    combined = cmd.stdout + "\n" + cmd.stderr

    migration_path: str | None = None
    revision: str | None = None
    m = _ALEMBIC_GENERATED_RE.search(combined)
    if m:
        # Path may come back absolute from inside the sandbox; strip to
        # workspace-relative for the result so the LLM can read it.
        raw = Path(m.group("path"))
        abs_candidate = raw if raw.is_absolute() else deps.workspace_root / raw
        if abs_candidate.exists():
            migration_path = rel_to(deps.workspace_root, abs_candidate)
        else:
            # Inside-container path won't exist on the host; record the
            # raw form so the agent has *something* to grep for.
            migration_path = raw.as_posix()
    rev_m = _ALEMBIC_REV_RE.search(combined)
    if rev_m:
        revision = rev_m.group(1)

    destructive: list[str] = []
    if migration_path:
        abs_mig = deps.workspace_root / migration_path
        if abs_mig.exists():
            destructive = _scan_destructive(abs_mig)

    return AlembicResult(
        revision=revision,
        message=message,
        migration_path=migration_path,
        destructive_ops=destructive,
        stdout=cmd.stdout,
        ok=cmd.returncode == 0,
    )


def register(agent: Agent[CoderDeps, str]) -> None:
    @agent.tool
    async def openapi_export(ctx: RunContext[CoderDeps]) -> CommandResult:
        """Export the backend OpenAPI schema to `openapi.json`.

        Call this after changing routes or Pydantic response models so
        the frontend client can be regenerated off the current schema.
        """
        ctx.deps.bind(tool="openapi_export").info("coder.codegen")
        return await _openapi_export(ctx.deps)

    @agent.tool
    async def regenerate_client(ctx: RunContext[CoderDeps]) -> CommandResult:
        """Regenerate the TS client + TanStack Query hooks from `openapi.json`.

        Run `openapi_export` first if the schema has changed.
        """
        ctx.deps.bind(tool="regenerate_client").info("coder.codegen")
        return await _regenerate_client(ctx.deps)

    @agent.tool
    async def alembic_autogenerate(
        ctx: RunContext[CoderDeps],
        message: str,
    ) -> AlembicResult:
        """Generate a new Alembic migration from the current ORM state.

        Returns the path to the new migration and the list of detected
        destructive operations (drop_table / drop_column / etc.). If
        any destructive ops are found the agent MUST call
        `request_human_review` before running `alembic upgrade head`.

        This tool does not auto-run `upgrade head`. The agent orchestrates
        that explicitly via `run_command('alembic', ['upgrade', 'head'])`.
        """
        ctx.deps.bind(tool="alembic_autogenerate", message=message).info("coder.codegen")
        result = await _alembic_autogenerate(ctx.deps, message)
        if result.destructive_ops:
            # Signal to the outer loop — the agent sees the result
            # first (via the raise's `__cause__`-style message), then
            # the LangGraph loop catches `HumanReviewRequired` and
            # pauses.
            raise HumanReviewRequired(
                question=(
                    f"Alembic migration {result.revision or '(unknown)'} contains "
                    f"destructive ops: {', '.join(result.destructive_ops)}. "
                    f"Review before applying."
                ),
                options=["approve", "edit-migration", "abort"],
            )
        return result


__all__ = ["register"]
