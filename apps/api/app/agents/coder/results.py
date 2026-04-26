"""Structured result types the Coder Agent's tools return to the model.

Why Pydantic models (not plain dicts)? Pydantic AI serialises tool return
values into JSON for the model. Typed results mean:

* The LLM sees a consistent shape per tool (easier in-context learning).
* We can audit/log every tool return in Langfuse with a known schema.
* Ruff + mypy can catch call-site mistakes instead of us debugging a
  tool that quietly returned `{"succes": True}` (typo) and confused the
  model for a whole turn.

Every result is serialisable via `.model_dump()` — no side-channel state.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FileListEntry(BaseModel):
    path: str
    is_dir: bool
    size_bytes: int | None = None


class FileList(BaseModel):
    """Result of `list_files`."""

    root: str = Field(description="The path the listing was rooted at, relative to workspace.")
    entries: list[FileListEntry]
    truncated: bool = Field(
        default=False,
        description="True if we hit the per-call entry cap and omitted some entries.",
    )


class FileRead(BaseModel):
    """Result of `read_file`.

    `content` is the exact bytes decoded as utf-8 (errors replaced).
    `line_count` is the total number of lines in the file — useful when
    the model needs to figure out whether its `end_line` request was clipped.
    """

    path: str
    content: str
    start_line: int
    end_line: int
    line_count: int
    clipped: bool = False


class WriteResult(BaseModel):
    """Result of `write_file`."""

    path: str
    bytes_written: int
    created: bool = Field(description="True if the file didn't exist before this call.")


class PatchHunkResult(BaseModel):
    """Per-hunk outcome from `apply_patch` — always populated, success or not."""

    hunk_index: int
    applied: bool
    reason: str | None = Field(
        default=None,
        description="Human-readable reason for failed hunks; None on success.",
    )
    matched_line: int | None = Field(
        default=None,
        description="1-indexed line where the hunk anchored in the resulting file.",
    )


class PatchResult(BaseModel):
    """Result of `apply_patch`.

    `ok=False` + populated `hunks` lets the model see which parts of its
    patch missed; it can then re-issue a narrower patch rather than
    resending the whole file.
    """

    path: str
    ok: bool
    hunks_applied: int
    hunks_total: int
    hunks: list[PatchHunkResult]
    bytes_written: int = 0


class SearchHit(BaseModel):
    path: str
    line: int
    content: str = Field(description="The matching line, trimmed of trailing whitespace.")


class SearchHits(BaseModel):
    """Result of `search_code`."""

    query: str
    hits: list[SearchHit]
    truncated: bool = False


class AstSymbol(BaseModel):
    """One symbol from `ast_summary`.

    `kind` is one of: class, function, async_function, pydantic_model,
    import. Extend as we grow the TS-side summariser.
    """

    kind: str
    name: str
    line: int
    signature: str | None = None


class AstSummary(BaseModel):
    """Result of `ast_summary`."""

    path: str
    language: str = Field(description="Detected language, e.g. 'python', 'typescript'.")
    symbols: list[AstSymbol]


class CommandResult(BaseModel):
    """Result of `run_command` and helpers that shell out.

    `stdout`/`stderr` are truncated at 8 KB each to protect the model's
    context window — the full output is persisted to Langfuse traces.
    """

    command: str
    returncode: int
    stdout: str
    stderr: str
    truncated: bool = False
    duration_s: float


class ValidatorIssue(BaseModel):
    """A single lint/type/test diagnostic."""

    tool: str = Field(description="e.g. 'ruff', 'mypy', 'pytest', 'tsc', 'eslint', 'vitest'.")
    path: str | None = None
    line: int | None = None
    code: str | None = None
    message: str


class ValidatorReport(BaseModel):
    """Result of `run_validators`."""

    ok: bool
    issue_count: int
    issues: list[ValidatorIssue]
    commands: list[CommandResult] = Field(
        description="Per-tool raw outcomes, in the order they ran.",
    )


class AlembicResult(BaseModel):
    """Result of `alembic_autogenerate`.

    `destructive_ops` lists any `op.drop_*` / `op.rename_*` operations
    found in the migration. The Coder Agent MUST call
    `request_human_review` before applying a migration whose
    `destructive_ops` is non-empty.

    `stdout`, `stderr`, and `returncode` mirror the underlying alembic
    invocation so the agent has a real diagnostic to read on failure.
    Pre-12th-regression we only surfaced `stdout`, which meant a
    failed autogenerate (returncode != 0) gave the agent no error
    message — alembic prints almost everything to stderr — and the
    agent fabricated theories about the cause (e.g. claiming env.py's
    `from app.models import SQLModel` import was wrong when it
    wasn't). Always include both streams.
    """

    revision: str | None
    message: str
    migration_path: str | None
    destructive_ops: list[str] = Field(default_factory=list)
    stdout: str
    stderr: str = ""
    returncode: int = 0
    ok: bool


class GitCommitResult(BaseModel):
    """Result of `git_commit`."""

    sha: str | None
    message: str
    files_changed: int
    nothing_to_commit: bool = False


class HumanReviewRequested(BaseModel):
    """Tool return for `request_human_review` — paired with raising
    `HumanReviewRequired` so the outer loop halts.

    We return this *and* raise so the agent's last observed tool event
    in the Langfuse trace is the question, not an unhandled exception.
    """

    question: str
    options: list[str] = Field(default_factory=list)


class ValidatorLoopAttempt(BaseModel):
    """One turn of the Coder Agent + the validator run that followed it.

    Kept flat (no nested agent messages) so the full loop result stays
    serialisable into Langfuse / Postgres without exploding payload size.
    `agent_output` is the agent's final string for the attempt;
    `agent_turn_count` is the number of ModelRequest/ModelResponse
    exchanges (for debugging run-on turns).
    """

    attempt: int = Field(description="1-indexed attempt number.")
    agent_output: str
    agent_turn_count: int
    # None when `agent_error` is set — the agent crashed before we could
    # run validators for the attempt, so there's nothing to report.
    report: ValidatorReport | None = None
    # Populated only when the agent errored out instead of returning
    # cleanly — e.g. pydantic-ai ran out of retries on a tool call and
    # surfaced the underlying error. Distinct from validator failure.
    agent_error: str | None = None


class ValidatorLoopResult(BaseModel):
    """Outcome of `run_task_with_validators`.

    `ok=True` iff the final attempt's validator run passed. `attempts`
    holds every attempt in order so the LangGraph outer loop can surface
    the progression to the UI and Langfuse.
    """

    ok: bool
    attempts_used: int
    max_attempts: int
    attempts: list[ValidatorLoopAttempt]
    final_report: ValidatorReport | None = Field(
        default=None,
        description=(
            "Shortcut to `attempts[-1].report` when at least one attempt "
            "ran; None if the loop bailed before even calling the agent."
        ),
    )
