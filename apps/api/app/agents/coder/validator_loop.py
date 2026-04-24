"""Coder Agent + validator retry loop — the orchestrator that owns #23.

One BuildPlan task → one call to `run_task_with_validators()`. The loop
runs the Coder Agent, runs the validator suite (roadmap §3: "ruff + mypy
+ pytest" for Python, "tsc + eslint + vitest" for frontend), and feeds
the top-N diagnostics back as a targeted retry prompt if anything failed.

Why a separate module and not the agent's own loop? Two reasons.

1. **Separation of concerns.** pydantic-ai's built-in `retries=N` is for
   schema-validation retries on a *single tool call* — it retries within
   one agent run. The retry we need here is "agent produced syntactically
   valid output but the validator rejected the result; run the agent
   again with diagnostics" — that's a whole-task retry, not a tool-call
   retry. Conflating them would make the agent's retry-on-bad-args path
   compete with our retry-on-failed-build path.

2. **LangGraph compatibility.** The outer build loop (#24) wants to
   pause, persist, resume, and time-travel across BuildPlan tasks. A
   plain async function with typed inputs/outputs drops into a LangGraph
   node trivially; an agent-internal loop would hide state inside
   pydantic-ai's graph machinery where LangGraph can't observe it.

Retry semantics:

* `max_attempts=3` per task. Each attempt = one `agent.run()` + one
  `run_validators()`.
* Retry prompt lists the top-20 diagnostics, explicitly telling the
  agent to fix those specific errors and not to refactor unrelated code.
  The roadmap's validator-loop spec uses the phrase *"fix these specific
  errors; do not refactor unrelated code"* — we keep that exact wording
  to match the system-prompt tone.
* `message_history` from the prior attempt is passed in so the agent
  doesn't re-discover the file tree each loop.
* `HumanReviewRequired` propagates out immediately — LangGraph pauses
  the whole build, so we don't want to burn retries on a question the
  agent has explicitly flagged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.agents.coder.agent import build_coder_agent
from app.agents.coder.context import CoderDeps
from app.agents.coder.errors import HumanReviewRequired
from app.agents.coder.results import (
    ValidatorIssue,
    ValidatorLoopAttempt,
    ValidatorLoopResult,
    ValidatorReport,
)
from app.agents.coder.tools.validators import run_validators

if TYPE_CHECKING:
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelMessage


# How many issues we surface in the retry prompt. Roadmap §3 says
# "top-N diagnostics" — 20 fits comfortably in a retry prompt (~1.5K
# tokens at most) and is enough signal for the agent to fix a typical
# batch of type/lint errors in one go. Beyond 20 the signal-to-noise
# drops; the agent tends to thrash.
_TOP_K_ISSUES = 20


def _format_issue(issue: ValidatorIssue) -> str:
    """Render one diagnostic as a single-line bullet for the retry prompt.

    `tool:path:line code — message`. Elide any field that's None so we
    don't leak stray colons or the literal string "None".
    """
    parts = [issue.tool]
    loc = issue.path or ""
    if issue.line:
        loc = f"{loc}:{issue.line}" if loc else f"line {issue.line}"
    if loc:
        parts.append(loc)
    if issue.code:
        parts.append(issue.code)
    head = " ".join(parts)
    return f"- {head} — {issue.message}" if head else f"- {issue.message}"


def _build_retry_prompt(report: ValidatorReport, attempt: int, max_attempts: int) -> str:
    """Turn a failed ValidatorReport into a targeted retry instruction.

    Deliberately terse — the agent already has the full file context from
    the prior attempt's messages. Listing diagnostics alone is more token-
    efficient than re-stating the task.
    """
    top_issues = report.issues[:_TOP_K_ISSUES]
    more = report.issue_count - len(top_issues)
    bullets = "\n".join(_format_issue(i) for i in top_issues)
    suffix = f"\n(+{more} more issues omitted)" if more > 0 else ""
    return (
        f"Attempt {attempt}/{max_attempts} — the validators rejected the last edit.\n"
        "Fix these specific errors; do not refactor unrelated code.\n"
        "\n"
        "<issues>\n"
        f"{bullets}{suffix}\n"
        "</issues>\n"
        "\n"
        "Use apply_patch for edits, not write_file. When you're done, "
        "commit with git_commit."
    )


async def _run_agent_once(
    agent: "Agent[CoderDeps, str]",
    prompt: str,
    deps: CoderDeps,
    message_history: list["ModelMessage"] | None,
) -> tuple[str, list["ModelMessage"], str | None]:
    """Invoke the Coder Agent once. Returns `(output, messages, error)`.

    On crash we return the exception repr as `error` and the prompt as
    `output`, leaving `messages` as whatever we had prior — so the loop
    can continue the conversation on the next attempt without losing
    context from successful earlier turns.
    """
    try:
        if message_history is not None:
            result = await agent.run(prompt, deps=deps, message_history=message_history)
        else:
            result = await agent.run(prompt, deps=deps)
        return result.output, list(result.all_messages()), None
    except HumanReviewRequired:
        # Let these propagate — LangGraph pauses the entire build and
        # surfaces the question to the user. Burning retries on them
        # would be both pointless and expensive.
        raise
    except Exception as exc:  # noqa: BLE001 — we want every other failure reported
        return prompt, list(message_history or []), repr(exc)


async def run_task_with_validators(
    task_prompt: str,
    deps: CoderDeps,
    *,
    validator_targets: list[str] | None = None,
    max_attempts: int = 3,
    agent: "Agent[CoderDeps, str] | None" = None,
) -> ValidatorLoopResult:
    """Run one BuildPlan task with automatic validator-feedback retries.

    Args:
        task_prompt:
            The natural-language task description (from the Planner
            Agent, typically something like "Add a `User` Pydantic model
            in apps/api/app/models/user.py with id/email/created_at").
        deps:
            Coder Agent dependencies — sandbox handle, logger, workspace
            root. The loop does not clone this; every attempt shares the
            same `deps` so the agent sees the same filesystem state
            the validators see.
        validator_targets:
            Names of validator suites to run after each attempt. Matches
            the keys of `tools.validators.TARGETS` — `["python"]`,
            `["python", "python-tests"]`, `["python", "frontend"]`, etc.
            Default is `["python"]`.
        max_attempts:
            Hard cap on agent+validator cycles for this task. Default 3
            per roadmap. If the validators still fail after this many
            passes we return `ok=False` and let the outer loop decide
            whether to escalate (human review vs. abandon the task).
        agent:
            Optional injected agent — tests pass a FunctionModel-backed
            agent here so they don't need Azure creds. In production
            this stays None and we use the lru_cached singleton.

    Returns:
        `ValidatorLoopResult` with per-attempt history. `ok=True` iff the
        final attempt's validator run returned `ok=True`.

    Raises:
        `HumanReviewRequired`: the agent (or a tool it called, like
        `alembic_autogenerate` on a destructive migration) asked to
        pause. Propagated so the LangGraph outer loop can handle it.

    Design note — why we don't skip validators if `target=[]`:
        We keep the contract simple: one attempt always = one agent run +
        one validator run. If you want to bypass validators, pass a
        target that's cheap but always green (e.g. a custom "noop"
        target registered in TARGETS). Bypassing adds an edge case at
        the API boundary for no real win.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")

    if agent is None:
        agent = build_coder_agent()

    targets = validator_targets or ["python"]
    attempts: list[ValidatorLoopAttempt] = []
    message_history: list["ModelMessage"] | None = None
    current_prompt = task_prompt

    log = deps.bind(
        loop="coder.validator_loop",
        max_attempts=max_attempts,
        targets=targets,
    )
    log.info("validator_loop.start", task_prompt_bytes=len(task_prompt))

    for attempt_idx in range(1, max_attempts + 1):
        # --- 1. Run the agent for this attempt ---
        output, new_messages, agent_error = await _run_agent_once(
            agent, current_prompt, deps, message_history
        )
        message_history = new_messages
        turn_count = _count_turns(new_messages)

        if agent_error is not None:
            # Agent crashed; record and retry with a generic nudge.
            attempts.append(
                ValidatorLoopAttempt(
                    attempt=attempt_idx,
                    agent_output=output,
                    agent_turn_count=turn_count,
                    report=None,
                    agent_error=agent_error,
                )
            )
            log.warning(
                "validator_loop.agent_error",
                attempt=attempt_idx,
                error=agent_error,
            )
            # Final attempt — bail out with ok=False.
            if attempt_idx >= max_attempts:
                break
            # Next attempt: retry with a neutral nudge. Don't prescribe
            # a specific tool — the last crash may well have been in
            # apply_patch itself, in which case telling the model to
            # prefer apply_patch is actively misleading. The model's
            # own retry-hint context is already in message_history if
            # pydantic-ai produced any.
            current_prompt = (
                f"The previous attempt failed with: {agent_error}\n"
                "Try a different approach. Re-read any file you're about "
                "to edit so your patch context matches the current "
                "contents exactly, and fall back to write_file for "
                "brand-new files."
            )
            continue

        # --- 2. Run validators ---
        report = await run_validators(deps, list(targets))
        attempts.append(
            ValidatorLoopAttempt(
                attempt=attempt_idx,
                agent_output=output,
                agent_turn_count=turn_count,
                report=report,
                agent_error=None,
            )
        )

        if report.ok:
            log.info(
                "validator_loop.success",
                attempt=attempt_idx,
                turn_count=turn_count,
            )
            return ValidatorLoopResult(
                ok=True,
                attempts_used=attempt_idx,
                max_attempts=max_attempts,
                attempts=attempts,
                final_report=report,
            )

        log.info(
            "validator_loop.attempt_failed",
            attempt=attempt_idx,
            issue_count=report.issue_count,
        )

        # --- 3. Prep retry prompt for next attempt ---
        if attempt_idx < max_attempts:
            current_prompt = _build_retry_prompt(report, attempt_idx + 1, max_attempts)

    # Loop exhausted. `attempts` is non-empty (max_attempts >= 1).
    final_report = attempts[-1].report if attempts else None
    log.info(
        "validator_loop.exhausted",
        attempts_used=len(attempts),
        final_issue_count=final_report.issue_count if final_report else None,
    )
    return ValidatorLoopResult(
        ok=False,
        attempts_used=len(attempts),
        max_attempts=max_attempts,
        attempts=attempts,
        final_report=final_report,
    )


def _count_turns(messages: list["ModelMessage"]) -> int:
    """Count ModelResponse entries in the message log.

    Approximates "how many times did the model speak this attempt" — the
    validator-loop log uses this to flag runaway agents (e.g. an agent
    that went 30 turns before giving up is a prompt or tool-schema bug,
    not a normal turn).
    """
    # Imported lazily — pydantic_ai's messages submodule costs ~40 ms on
    # cold import, and we only count when we have an attempt to record.
    from pydantic_ai.messages import ModelResponse

    return sum(1 for m in messages if isinstance(m, ModelResponse))


__all__ = ["run_task_with_validators"]


# Re-export for unit tests that want to probe prompt construction.
_build_retry_prompt_for_tests: Any = _build_retry_prompt
_format_issue_for_tests: Any = _format_issue
