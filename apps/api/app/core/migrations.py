"""Programmatic Alembic helpers for the gateway's own Postgres.

Two responsibilities:

* `current_head_state()` — compare the DB's current revision to the
  on-disk script tree. Read-only; safe to call from any environment.
* `upgrade_to_head()` — `alembic upgrade head` without shelling out to
  the CLI binary. Used in lifespan for `ENVIRONMENT == "local"` so a
  fresh dev DB doesn't surface as `relation "build_runs" does not
  exist` halfway through a build stream. Production lifespan only
  *checks* — actual upgrades are gated on a deploy-time job.

We intentionally use the **sync** psycopg URL (`DATABASE_URL_SYNC`) for
this — Alembic's whole API is sync, and `run_in_executor` is cheaper
than dragging in async-alembic shims.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import structlog
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from app.core.config import settings

log = structlog.get_logger(__name__)

# `apps/api/alembic` lives next to `app/`. We compute the path once at
# import so workers running from any cwd still find the script tree.
_ALEMBIC_DIR = Path(__file__).resolve().parents[2] / "alembic"


@dataclass(frozen=True)
class MigrationState:
    """Snapshot of DB-vs-disk migration state.

    `up_to_date` is True iff the DB's current revision matches the
    head of the on-disk script tree. `pending` lists revisions on
    disk newer than the DB — non-empty means the operator has work
    to do.
    """

    db_revision: str | None
    head_revision: str | None
    pending: list[str]

    @property
    def up_to_date(self) -> bool:
        return self.db_revision == self.head_revision and not self.pending


def _alembic_config() -> Config:
    """Build an in-memory Alembic Config pointing at our script tree.

    No `alembic.ini` exists in the repo — keep it that way. Anything
    Alembic needs is set explicitly here so config is co-located with
    code and survives container layouts that don't ship the ini file.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", str(settings.DATABASE_URL_SYNC))
    return cfg


def _state_sync() -> MigrationState:
    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()

    engine = create_engine(str(settings.DATABASE_URL_SYNC), pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            current = ctx.get_current_revision()
    finally:
        engine.dispose()

    # `iterate_revisions(head, current)` walks newest → oldest *exclusive*
    # of `current`. Empty list means "no work to do."
    pending: list[str] = []
    if current != head:
        for rev in script.iterate_revisions(head, current):
            pending.append(rev.revision)
    return MigrationState(db_revision=current, head_revision=head, pending=pending)


def _upgrade_sync() -> None:
    # Lazily import `command` — keeps Alembic's CLI path off the hot
    # import graph for non-local deployments that never call this.
    from alembic import command

    cfg = _alembic_config()
    command.upgrade(cfg, "head")


async def current_head_state() -> MigrationState:
    """Async wrapper — Alembic is sync, so we offload to a thread."""
    return await asyncio.to_thread(_state_sync)


async def upgrade_to_head() -> None:
    """Run `alembic upgrade head` against the gateway DB. Local only."""
    await asyncio.to_thread(_upgrade_sync)


async def ensure_schema_for_environment() -> None:
    """Lifespan hook: keep dev databases self-healing, fail loud in prod.

    * `local` — auto-`upgrade head`. Lowers friction; the alternative
      is `relation "build_runs" does not exist` halfway through an SSE
      stream, which is a terrible developer experience.
    * `staging` / `production` — *check* only. If migrations are
      pending we log a structured error so it shows up in Sentry; we
      do not silently mutate the prod schema. Boot continues so health
      checks can still answer (the operator wants to see the alert,
      not a CrashLoopBackOff).
    """
    if settings.ENVIRONMENT == "local":
        try:
            state = await current_head_state()
        except Exception as exc:  # noqa: BLE001 — best-effort, see below
            # If the DB isn't reachable yet (compose race, fresh
            # container) we just log and continue — the first request
            # will produce a clearer connection error than we can.
            log.warning("alloy.migrations.state_check_failed", error=str(exc))
            return
        if state.up_to_date:
            log.info("alloy.migrations.up_to_date", revision=state.db_revision)
            return
        log.info(
            "alloy.migrations.upgrading",
            from_=state.db_revision,
            to=state.head_revision,
            pending=state.pending,
        )
        try:
            await upgrade_to_head()
        except Exception as exc:  # noqa: BLE001 — best-effort
            # Don't take down the API; let the operator see the SSE
            # `event: error` envelope at /build/run with the migration
            # hint instead of a CrashLoopBackOff.
            log.error("alloy.migrations.upgrade_failed", error=str(exc))
            return
        log.info("alloy.migrations.upgrade_complete", revision=state.head_revision)
        return

    # Non-local: check, don't mutate.
    try:
        state = await current_head_state()
    except Exception as exc:  # noqa: BLE001
        log.error("alloy.migrations.state_check_failed", error=str(exc))
        return
    if state.up_to_date:
        log.info("alloy.migrations.up_to_date", revision=state.db_revision)
        return
    log.error(
        "alloy.migrations.pending",
        environment=settings.ENVIRONMENT,
        db_revision=state.db_revision,
        head_revision=state.head_revision,
        pending=state.pending,
        hint="Run `alembic upgrade head` from apps/api before serving traffic.",
    )
