"""`python -m scripts.run_coder_task` — run one BuildPlan task locally.

A thin CLI around `app.agents.coder.run_task_with_validators` so we can
exercise the Coder Agent + validator retry loop against a real workspace
without standing up the gateway, the queue, or a sandbox. Primarily used
to debug the LangGraph outer loop (#24) in isolation and to reproduce
validator-loop regressions the Langfuse trace flagged.

Usage:
    cd apps/api
    uv run python -m scripts.run_coder_task "Add a User model..."

    # Point at a different workspace + narrow validator targets:
    uv run python -m scripts.run_coder_task \\
        --workspace ../../tmp/demo-project \\
        --targets python --targets python-tests \\
        --max-attempts 3 \\
        "Fix the mypy errors in app/models/user.py"

Env required: same as the API — `AZURE_OPENAI_ENDPOINT`,
`AZURE_OPENAI_DEPLOYMENT` (or the LiteLLM fallback cascade). The script
loads `.env` from `apps/api/.env` automatically via pydantic-settings,
mirroring the way `uvicorn` starts the real gateway.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Annotated

import typer

from app.agents.coder import run_task_with_validators
from app.agents.coder.context import CoderDeps
from app.agents.coder.errors import HumanReviewRequired

app = typer.Typer(
    add_completion=False,
    help="Run a single Coder Agent task with the validator retry loop.",
)

_DEFAULT_WORKSPACE = Path(__file__).resolve().parents[3]  # repo root


@app.command()
def main(
    task: Annotated[
        str,
        typer.Argument(
            help="Natural-language task description, as the Planner Agent "
            "would hand it to the Coder Agent.",
        ),
    ],
    workspace: Annotated[
        Path,
        typer.Option(
            "--workspace",
            "-w",
            help="Workspace root the agent should treat as the sandbox. "
            "Defaults to the Alloy repo root.",
            show_default=False,
        ),
    ] = _DEFAULT_WORKSPACE,
    targets: Annotated[
        list[str] | None,
        typer.Option(
            "--targets",
            "-t",
            help="Validator targets to run after each attempt. Repeat for "
            "multiple. Valid: python, python-tests, frontend, "
            "frontend-tests. Default: python.",
        ),
    ] = None,
    max_attempts: Annotated[
        int,
        typer.Option(
            "--max-attempts",
            "-n",
            min=1,
            max=10,
            help="Hard cap on agent+validator cycles.",
        ),
    ] = 3,
    project_id: Annotated[
        str | None,
        typer.Option(
            "--project-id",
            help="Correlation ID written into structlog + Langfuse traces.",
        ),
    ] = None,
    json_out: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit the ValidatorLoopResult as JSON instead of a summary.",
        ),
    ] = False,
) -> None:
    """Run the Coder Agent for one task and print the loop result."""
    workspace = workspace.expanduser().resolve()
    if not workspace.is_dir():
        typer.echo(f"workspace not found or not a directory: {workspace}", err=True)
        raise typer.Exit(code=2)

    deps = CoderDeps(
        workspace_root=workspace,
        turn_id=uuid.uuid4().hex[:12],
        project_id=project_id or "cli-run",
    )

    typer.echo(
        f"→ Running Coder Agent · workspace={workspace} · "
        f"max_attempts={max_attempts} · targets={targets or ['python']}",
        err=True,
    )

    try:
        result = asyncio.run(
            run_task_with_validators(
                task,
                deps,
                validator_targets=targets,
                max_attempts=max_attempts,
            )
        )
    except HumanReviewRequired as exc:
        # Expected outcome when the agent flags a destructive op — we
        # don't have a human here, so print the question and exit 3.
        typer.echo(f"HUMAN REVIEW REQUIRED: {exc.question}", err=True)
        if exc.options:
            typer.echo(f"options: {', '.join(exc.options)}", err=True)
        raise typer.Exit(code=3) from exc

    if json_out:
        # `model_dump(mode='json')` yields something orjson-compatible.
        print(json.dumps(result.model_dump(mode="json"), indent=2, default=str))
    else:
        status = "OK" if result.ok else "FAILED"
        typer.echo(
            f"\n=== {status} · attempts {result.attempts_used}/{result.max_attempts} ===",
            err=True,
        )
        for attempt in result.attempts:
            header = f"[attempt {attempt.attempt}] turns={attempt.agent_turn_count}"
            if attempt.agent_error:
                typer.echo(f"{header} · AGENT ERROR: {attempt.agent_error}", err=True)
                continue
            report = attempt.report
            if report is None:
                typer.echo(f"{header} · no report", err=True)
                continue
            typer.echo(
                f"{header} · validators {'OK' if report.ok else 'FAIL'} "
                f"· {report.issue_count} issues",
                err=True,
            )
            for issue in report.issues[:10]:
                loc = ""
                if issue.path:
                    loc = issue.path
                    if issue.line:
                        loc += f":{issue.line}"
                code = f" {issue.code}" if issue.code else ""
                typer.echo(f"    - {issue.tool}{code} {loc} — {issue.message}", err=True)

        # Final agent output (last successful attempt's English summary).
        last = next(
            (a for a in reversed(result.attempts) if a.agent_error is None), None
        )
        if last is not None and last.agent_output:
            typer.echo("\nAgent summary:\n" + last.agent_output)

    # Exit code mirrors loop outcome so shell pipelines / CI can branch.
    raise typer.Exit(code=0 if result.ok else 1)


if __name__ == "__main__":
    # Running `python scripts/run_coder_task.py ...` directly should work
    # the same as `python -m scripts.run_coder_task ...`.
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)
