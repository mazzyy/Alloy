"""Tests for `app.services.projects`.

Focuses on the slug-collision bug surfaced in production: two different
prompts that slugify to the same value within a tenant used to crash
`get_or_create_project` with `asyncpg.UniqueViolationError` on the
`uq_projects_tenant_slug` index. The fix probes for the next free slug
suffix before insert; these tests pin that behaviour.

We avoid spinning up Postgres (the metadata schema uses JSONB which
SQLite can't host) by stubbing the AsyncSession with a small fake that
implements just the surface `get_or_create_project` exercises.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from app.models.project import Project
from app.services import projects as projects_mod


# ── Test double ────────────────────────────────────────────────────────


class _FakeScalars:
    """Mimics SQLAlchemy's `Result.scalars()` / `.scalar_one_or_none()`."""

    def __init__(self, values: Sequence[Any]) -> None:
        self._values = list(values)

    def all(self) -> list[Any]:
        return list(self._values)

    def scalar_one_or_none(self) -> Any:
        return self._values[0] if self._values else None


class _FakeResult:
    def __init__(self, values: Sequence[Any]) -> None:
        self._values = list(values)

    def scalar_one_or_none(self) -> Any:
        return self._values[0] if self._values else None

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._values)


class FakeProjectsSession:
    """In-memory stand-in for AsyncSession.

    Holds a list of `Project` rows and answers the two queries
    `get_or_create_project` makes:

    * SELECT by `(tenant_id, original_prompt)` for prompt-dedup.
    * SELECT slug starting-with `<base>` for collision probing.

    `add()` + `flush()` simulate insert. Anything the function tries to
    do beyond that raises — keeps the stub honest if the production code
    grows new query shapes.
    """

    def __init__(self, existing: list[Project] | None = None) -> None:
        self._rows: list[Project] = list(existing or [])
        self.executed_statements: list[str] = []

    async def execute(self, stmt: Any) -> _FakeResult:
        # We can't structurally introspect the SQLAlchemy `Select` here
        # without a compile, so we rasterise by inspecting the WHERE
        # criteria's column names + the chosen entity. Cheap and good
        # enough — production has integration coverage on top.
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        self.executed_statements.append(compiled)

        # Branch discriminator: the dedup query has a WHERE on
        # `original_prompt`; the slug-collision query has a WHERE on
        # `slug LIKE ...`. Both have a WHERE on `tenant_id`.
        upper = compiled.upper()
        has_prompt_filter = "ORIGINAL_PROMPT" in upper and "=" in upper
        has_slug_like = "SLUG LIKE" in upper or "SLUG  LIKE" in upper or (
            "LIKE" in upper and "SLUG" in upper
        )

        if has_prompt_filter and not has_slug_like:
            tenant_id = _extract_param(compiled, "tenant_id")
            prompt = _extract_param(compiled, "original_prompt")
            matches = [
                p
                for p in self._rows
                if p.tenant_id == tenant_id and p.original_prompt == prompt
            ]
            return _FakeResult(matches[-1:])  # most-recent-only

        if has_slug_like:
            tenant_id = _extract_param(compiled, "tenant_id")
            prefix = _extract_like_prefix(compiled)
            slugs = [
                p.slug
                for p in self._rows
                if p.tenant_id == tenant_id and p.slug.startswith(prefix)
            ]
            return _FakeResult(slugs)

        raise AssertionError(f"unexpected query in fake session: {compiled!r}")

    def add(self, row: Any) -> None:
        if not isinstance(row, Project):
            raise AssertionError(f"unexpected add({row!r})")
        # Enforce the (tenant_id, slug) unique constraint the same way
        # Postgres would — a passing test means we never reach this
        # branch because `_next_available_slug` resolved the conflict.
        for existing in self._rows:
            if existing.tenant_id == row.tenant_id and existing.slug == row.slug:
                raise AssertionError(
                    f"unique violation: ({row.tenant_id}, {row.slug}) already in fake session"
                )
        self._rows.append(row)

    async def flush(self) -> None:  # noqa: D401 — stub
        return None

    @property
    def rows(self) -> list[Project]:
        return list(self._rows)


def _extract_param(compiled_sql: str, param: str) -> str:
    """Pull `<param> = '<value>'` out of a literal-bound compiled query.

    Compiled output looks like `WHERE projects.tenant_id = 'dev_tenant'
    AND projects.original_prompt = 'Build a portfolio'`. We just regex
    the param name + its quoted literal.
    """
    import re

    m = re.search(rf"{param}\s*=\s*'([^']*)'", compiled_sql)
    if not m:
        raise AssertionError(f"could not find param {param!r} in: {compiled_sql!r}")
    return m.group(1)


def _extract_like_prefix(compiled_sql: str) -> str:
    """Pull the prefix out of `LIKE 'frontend-portfolio%' ESCAPE '/'`.

    SQLAlchemy's `startswith()` compiles to a LIKE with a trailing
    ESCAPE clause; we accept either form.
    """
    import re

    m = re.search(r"LIKE\s+'([^%']*)%'", compiled_sql, flags=re.IGNORECASE)
    if not m:
        raise AssertionError(f"could not find LIKE prefix in: {compiled_sql!r}")
    return m.group(1)


def _make_project(tenant_id: str, slug: str, prompt: str) -> Project:
    return Project(
        id=uuid4(),
        tenant_id=tenant_id,
        slug=slug,
        name=prompt[:200],
        original_prompt=prompt,
        created_at=datetime.now(UTC).replace(tzinfo=None),
        updated_at=datetime.now(UTC).replace(tzinfo=None),
    )


# ── Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_or_create_returns_existing_on_prompt_dedup() -> None:
    """Same prompt within the same tenant must return the original row."""
    existing = _make_project(
        "dev_tenant",
        "frontend-portfolio",
        "Build a frontend portfolio website",
    )
    session = FakeProjectsSession([existing])
    out = await projects_mod.get_or_create_project(
        session,  # type: ignore[arg-type]
        tenant_id="dev_tenant",
        prompt="Build a frontend portfolio website",
    )
    assert out.id == existing.id
    assert len(session.rows) == 1, "no insert should have happened on dedup hit"


@pytest.mark.asyncio
async def test_get_or_create_appends_suffix_on_slug_collision() -> None:
    """Different prompt that slugifies to the same value must NOT crash.

    Reproduces the production failure:
      "Build a frontend portfolio website"             → frontend-portfolio
      "Build a lightweight portfolio website based on  → frontend-portfolio
       frontend"                                        (collision)

    Expected behaviour: the second insert lands as `frontend-portfolio-2`.
    """
    first = _make_project(
        "dev_tenant",
        "frontend-portfolio",
        "Build a frontend portfolio website",
    )
    session = FakeProjectsSession([first])

    second = await projects_mod.get_or_create_project(
        session,  # type: ignore[arg-type]
        tenant_id="dev_tenant",
        prompt="Build a lightweight portfolio website based on frontend",
        name="Frontend Portfolio",
    )
    assert second.id != first.id
    assert second.slug == "frontend-portfolio-2", (
        f"expected suffixed slug, got {second.slug!r}"
    )
    assert len(session.rows) == 2


@pytest.mark.asyncio
async def test_get_or_create_walks_suffix_when_multiple_collisions() -> None:
    """Three colliding prompts → -2, -3."""
    rows = [
        _make_project("dev_tenant", "frontend-portfolio", "prompt 1"),
        _make_project("dev_tenant", "frontend-portfolio-2", "prompt 2"),
    ]
    session = FakeProjectsSession(rows)
    third = await projects_mod.get_or_create_project(
        session,  # type: ignore[arg-type]
        tenant_id="dev_tenant",
        prompt="prompt 3",
        name="Frontend Portfolio",
    )
    assert third.slug == "frontend-portfolio-3"


@pytest.mark.asyncio
async def test_get_or_create_isolates_slug_collisions_per_tenant() -> None:
    """Tenant A's "frontend-portfolio" must not block tenant B's."""
    rows = [_make_project("tenant_a", "frontend-portfolio", "prompt a")]
    session = FakeProjectsSession(rows)
    other = await projects_mod.get_or_create_project(
        session,  # type: ignore[arg-type]
        tenant_id="tenant_b",
        prompt="prompt b",
        name="Frontend Portfolio",
    )
    assert other.tenant_id == "tenant_b"
    assert other.slug == "frontend-portfolio", (
        f"tenant B should reuse the unsuffixed slug, got {other.slug!r}"
    )


@pytest.mark.asyncio
async def test_next_available_slug_returns_base_when_free() -> None:
    session = FakeProjectsSession([])
    slug = await projects_mod._next_available_slug(
        session,  # type: ignore[arg-type]
        tenant_id="dev_tenant",
        base="any-slug",
    )
    assert slug == "any-slug"
