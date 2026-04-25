"""Deterministic post-build finalisation.

Phase 1 lets the Coder Agent emit `backend.openapi_export`,
`frontend.client.codegen`, and the smoke-test ops as part of the plan,
but *relying on the LLM* to call those tools at the right time is
fragile — a tired model that stops at the last router op leaves the
generated frontend without a typed client and the DB without an applied
migration.

This module runs the same three steps deterministically *after* the
LangGraph loop returns successfully:

1. ``alembic upgrade head`` — apply any migrations the agent generated.
2. ``openapi.json`` export from the live backend container.
3. ``@hey-api/openapi-ts`` regen of the TS client + TanStack Query hooks.
4. ``playwright test --grep '@smoke'`` — best-effort smoke run.

Each step is best-effort: a failure here does not unwind the build
(the apps still exist on disk; the user can fix and re-run), but is
surfaced via the returned ``FinaliseReport`` so the API layer can
emit it as part of the SSE result envelope.

We do not run these as additional plan tasks because:

* They are not file ops — the FileOp model is the wrong shape for
  "run this command and record the result".
* Running them outside the validator loop avoids having `alembic
  upgrade head` get retried 3× when the real fix is "the user needs
  to set DATABASE_URL".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from app.agents.coder.context import CoderDeps
from app.agents.coder.results import CommandResult
from app.agents.coder.tools.commands import run_command

_log = structlog.get_logger("alloy.finalise")


@dataclass
class FinaliseStep:
    """One step of the finalisation pipeline + its result."""

    name: str
    ok: bool
    skipped: bool = False
    return_code: int | None = None
    stdout_tail: str | None = None
    stderr_tail: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "skipped": self.skipped,
            "return_code": self.return_code,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "error": self.error,
        }


@dataclass
class FinaliseReport:
    """Combined report — what `finalise_build` returns to the API layer."""

    ok: bool
    steps: list[FinaliseStep] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "steps": [s.to_dict() for s in self.steps],
        }


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


def _tail(text: str | None, n: int = 800) -> str | None:
    if not text:
        return None
    if len(text) <= n:
        return text
    return text[-n:]


def _step_from_command(
    *,
    name: str,
    cmd: CommandResult,
    treat_nonzero_as_failure: bool = True,
) -> FinaliseStep:
    ok = cmd.returncode == 0 if treat_nonzero_as_failure else True
    return FinaliseStep(
        name=name,
        ok=ok,
        return_code=cmd.returncode,
        stdout_tail=_tail(cmd.stdout),
        stderr_tail=_tail(cmd.stderr),
    )


def _alembic_present(workspace: Path) -> bool:
    """Detect a generated app that uses Alembic.

    Looks for the same anchor file the base template ships
    (`apps/api/alembic.ini`). Generated workspaces follow the same
    layout so this also works for projects scaffolded by Alloy.
    """
    return (workspace / "apps" / "api" / "alembic.ini").is_file() or (
        workspace / "backend" / "alembic.ini"
    ).is_file()


def _has_package_json(workspace: Path) -> bool:
    return (workspace / "apps" / "web" / "package.json").is_file() or (
        workspace / "frontend" / "package.json"
    ).is_file()


def _has_playwright(workspace: Path) -> bool:
    """Is Playwright installed somewhere in the generated frontend?"""
    candidates = [
        workspace / "apps" / "web" / "playwright.config.ts",
        workspace / "apps" / "web" / "playwright.config.js",
        workspace / "frontend" / "playwright.config.ts",
        workspace / "frontend" / "playwright.config.js",
        workspace / "playwright.config.ts",
    ]
    return any(c.is_file() for c in candidates)


# ────────────────────────────────────────────────────────────────────
# Public entry point
# ────────────────────────────────────────────────────────────────────


async def finalise_build(deps: CoderDeps) -> FinaliseReport:
    """Run the deterministic post-build pipeline.

    Steps that don't apply to the workspace (missing `alembic.ini`, no
    `package.json`, no Playwright config) are recorded as ``skipped``
    so the report still tells a complete story.
    """
    log = deps.bind(stage="finalise")
    workspace = deps.workspace_root
    report = FinaliseReport(ok=True)

    # 1. alembic upgrade head -------------------------------------------
    if _alembic_present(workspace):
        log.info("finalise.alembic.upgrade_head.start")
        # Pick the subdir that actually has alembic.ini — generated
        # projects from full-stack-fastapi-template put it in
        # `backend/`, our own monorepo would put it in `apps/api/`.
        alembic_subdir: str | None = None
        if (workspace / "backend" / "alembic.ini").is_file():
            alembic_subdir = "backend"
        elif (workspace / "apps" / "api" / "alembic.ini").is_file():
            alembic_subdir = "apps/api"
        try:
            cmd = await run_command(
                deps,
                "alembic",
                ["upgrade", "head"],
                timeout_s=120,
                cwd_subdir=alembic_subdir,
            )
            step = _step_from_command(name="alembic_upgrade", cmd=cmd)
        except Exception as exc:  # noqa: BLE001 — best-effort
            step = FinaliseStep(
                name="alembic_upgrade",
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        report.steps.append(step)
        if not step.ok:
            report.ok = False
            log.warning("finalise.alembic.upgrade_head.failed", step=step.to_dict())
    else:
        report.steps.append(
            FinaliseStep(name="alembic_upgrade", ok=True, skipped=True)
        )

    # 2. openapi.json export --------------------------------------------
    log.info("finalise.openapi.export.start")
    try:
        # Reuse the same script-or-fallback path the agent's tool uses.
        from app.agents.coder.tools.codegen import _openapi_export

        cmd = await _openapi_export(deps)
        step = _step_from_command(name="openapi_export", cmd=cmd)
    except Exception as exc:  # noqa: BLE001
        step = FinaliseStep(
            name="openapi_export",
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    report.steps.append(step)
    if not step.ok:
        report.ok = False
        log.warning("finalise.openapi.export.failed", step=step.to_dict())

    # 3. TS client regen ------------------------------------------------
    if _has_package_json(workspace):
        log.info("finalise.ts_client.regen.start")
        try:
            from app.agents.coder.tools.codegen import _regenerate_client

            cmd = await _regenerate_client(deps)
            step = _step_from_command(name="ts_client_regen", cmd=cmd)
        except Exception as exc:  # noqa: BLE001
            step = FinaliseStep(
                name="ts_client_regen",
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        report.steps.append(step)
        if not step.ok:
            report.ok = False
            log.warning("finalise.ts_client.regen.failed", step=step.to_dict())
    else:
        report.steps.append(
            FinaliseStep(name="ts_client_regen", ok=True, skipped=True)
        )

    # 4. Playwright smoke ----------------------------------------------
    if _has_playwright(workspace):
        log.info("finalise.playwright.smoke.start")
        try:
            cmd = await run_command(
                deps,
                "npx",
                ["playwright", "test", "--grep", "@smoke", "--reporter=line"],
                timeout_s=180,
            )
            # Smoke is non-blocking — Phase 1 ships the runner but treats
            # red smoke as a warning, not a build failure. Phase 4 turns
            # this into a hard gate once the templates' smoke suites
            # stabilise.
            step = _step_from_command(
                name="playwright_smoke",
                cmd=cmd,
                treat_nonzero_as_failure=False,
            )
            step.ok = cmd.returncode == 0  # report the truth, just don't fail the build
        except Exception as exc:  # noqa: BLE001
            step = FinaliseStep(
                name="playwright_smoke",
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        report.steps.append(step)
        # Note: report.ok intentionally NOT set False on smoke failure.
    else:
        report.steps.append(
            FinaliseStep(name="playwright_smoke", ok=True, skipped=True)
        )

    log.info(
        "finalise.complete",
        ok=report.ok,
        steps=[s.name for s in report.steps],
        skipped=[s.name for s in report.steps if s.skipped],
    )
    return report


__all__ = ["FinaliseReport", "FinaliseStep", "finalise_build"]
