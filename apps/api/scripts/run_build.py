"""`python -m scripts.run_build` — run a BuildPlan through the LangGraph loop.

Sibling of `run_coder_task.py` (single-task runner). This one drives the
full outer loop: reads a BuildPlan JSON, topologically orders the ops,
runs each one through `run_task_with_validators`, commits on green,
rolls back on red, and pauses on human review.

Usage:
    cd apps/api
    uv run python -m scripts.run_build path/to/plan.json

    # Resume a paused build after answering a human-review question:
    uv run python -m scripts.run_build path/to/plan.json \\
        --resume --thread-id project:demo-proj --answer "yes"

Exit codes:
    0  build completed, every task green
    1  build failed (at least one task exhausted retries)
    2  bad workspace / plan file
    3  build paused for human review (thread_id + question printed)
    4  invalid resume invocation (e.g. missing --thread-id)
    130 Ctrl-C

`--memory` swaps `AsyncSqliteSaver` for `MemorySaver` — useful during
early debugging when you don't want a `.alloy/checkpoints.sqlite` file
cluttering the workspace.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from alloy_shared.plan import BuildPlan
from app.agents.build import resume_build, run_build_plan
from app.agents.build.state import BuildOutcome
from app.agents.coder.context import CoderDeps

app = typer.Typer(
    add_completion=False,
    help="Run a full BuildPlan through the LangGraph build loop.",
)

_DEFAULT_WORKSPACE = Path(__file__).resolve().parents[3]  # repo root


def _print_outcome(outcome: BuildOutcome) -> None:
    """Human-readable summary. JSON mode prints the raw model_dump."""
    if outcome.pending_review is not None:
        typer.echo(
            f"\n=== PAUSED · task {outcome.pending_review.task_id} "
            f"requested human review ===",
            err=True,
        )
        typer.echo(f"question: {outcome.pending_review.question}", err=True)
        if outcome.pending_review.options:
            typer.echo(
                f"options:  {', '.join(outcome.pending_review.options)}", err=True
            )
        typer.echo(f"thread_id: {outcome.thread_id}", err=True)
        typer.echo(
            "\nResume with:\n"
            f"  uv run python -m scripts.run_build <plan.json> "
            f"--resume --thread-id {outcome.thread_id} --answer '<your answer>'",
            err=True,
        )
        return

    status = "OK" if outcome.ok else "FAILED"
    typer.echo(
        f"\n=== {status} · tasks {outcome.tasks_run}/{outcome.tasks_total} ===",
        err=True,
    )
    for o in outcome.outcomes:
        head = f"[{o.task_id}] attempts={o.attempts_used}"
        if o.ok:
            suffix = f"commit={o.commit_sha[:8]}" if o.commit_sha else "commit=(no writes)"
            typer.echo(f"  ✓ {head} · {suffix}", err=True)
        else:
            typer.echo(f"  ✗ {head} · {o.error or 'unknown failure'}", err=True)


@app.command()
def main(
    plan_file: Annotated[
        Path,
        typer.Argument(
            help="Path to a BuildPlan JSON file (as emitted by the Planner Agent).",
        ),
    ],
    workspace: Annotated[
        Path,
        typer.Option(
            "--workspace",
            "-w",
            help="Workspace root the build should operate on. Defaults to the Alloy repo root.",
            show_default=False,
        ),
    ] = _DEFAULT_WORKSPACE,
    targets: Annotated[
        list[str] | None,
        typer.Option(
            "--targets",
            "-t",
            help="Validator targets to run after each task. Repeat for multiple.",
        ),
    ] = None,
    max_attempts: Annotated[
        int,
        typer.Option(
            "--max-attempts",
            "-n",
            min=1,
            max=10,
            help="Per-task cap on agent+validator cycles.",
        ),
    ] = 3,
    project_id: Annotated[
        str | None,
        typer.Option(
            "--project-id",
            help="Project correlation ID. Also used to derive the LangGraph thread_id.",
        ),
    ] = None,
    memory: Annotated[
        bool,
        typer.Option(
            "--memory",
            help="Use the in-memory checkpointer instead of SqliteSaver. "
            "Avoids writing `.alloy/checkpoints.sqlite` — great for throwaway debug runs.",
        ),
    ] = False,
    resume: Annotated[
        bool,
        typer.Option(
            "--resume",
            help="Resume a paused build. Requires --thread-id and --answer.",
        ),
    ] = False,
    thread_id: Annotated[
        str | None,
        typer.Option(
            "--thread-id",
            help="LangGraph thread_id (printed by the paused run). Required with --resume.",
        ),
    ] = None,
    answer: Annotated[
        str | None,
        typer.Option(
            "--answer",
            help="Answer to the pending human-review question. Required with --resume.",
        ),
    ] = None,
    json_out: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit the BuildOutcome as JSON instead of a summary.",
        ),
    ] = False,
) -> None:
    """Run or resume a BuildPlan end-to-end."""
    workspace = workspace.expanduser().resolve()
    if not workspace.is_dir():
        typer.echo(f"workspace not found or not a directory: {workspace}", err=True)
        raise typer.Exit(code=2)

    if not plan_file.exists():
        typer.echo(f"plan file not found: {plan_file}", err=True)
        raise typer.Exit(code=2)

    try:
        plan = BuildPlan.model_validate_json(plan_file.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — exit with a clear message, not a traceback
        typer.echo(f"failed to parse plan file: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if resume and (thread_id is None or answer is None):
        typer.echo("--resume requires --thread-id and --answer", err=True)
        raise typer.Exit(code=4)

    deps = CoderDeps(
        workspace_root=workspace,
        project_id=project_id or "cli-build",
    )
    checkpointer_kind = "memory" if memory else "sqlite"

    typer.echo(
        f"→ {'Resuming' if resume else 'Running'} build · "
        f"workspace={workspace} · ops={len(plan.ops)} · "
        f"max_attempts={max_attempts} · ckpt={checkpointer_kind}",
        err=True,
    )

    async def _go() -> BuildOutcome:
        if resume:
            return await resume_build(
                answer=answer or "",
                deps=deps,
                thread_id=thread_id or "",
                max_attempts=max_attempts,
                validator_targets=targets,
                checkpointer_kind=checkpointer_kind,
            )
        return await run_build_plan(
            plan,
            deps,
            max_attempts=max_attempts,
            validator_targets=targets,
            checkpointer_kind=checkpointer_kind,
        )

    outcome = asyncio.run(_go())

    if json_out:
        print(json.dumps(outcome.model_dump(mode="json"), indent=2, default=str))
    else:
        _print_outcome(outcome)

    if outcome.pending_review is not None:
        raise typer.Exit(code=3)
    raise typer.Exit(code=0 if outcome.ok else 1)


if __name__ == "__main__":
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)
