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


# Below this many non-whitespace chars in `agent_output` *and* zero
# touched paths, we treat the attempt as a silent giveup rather than an
# intentional no-op completion. 80 chars is roughly "I did X and
# committed it." — anything substantially shorter is content-free.
_MIN_NO_WRITE_OUTPUT_CHARS = 80


# Phrases that signal the agent is rationalising a giveup as success.
# Hit during Phase-1 verification: an agent's `apply_patch` calls all
# failed, the agent then wrote a 350-char "I did not modify any app
# logic; I created a small checkpoint commit so future patches match
# the tree" summary, and the validator loop accepted it as an
# intentional no-op (because output_stripped >= 80 chars). The tell is
# always some combination of:
#   * an admission of non-action ("did not modify", "no changes made"),
#   * a "checkpoint commit" / "marker commit" alibi for the empty
#     touched_paths set,
#   * an apply_patch failure mention without a corresponding success.
#
# Match is case-insensitive substring. Curated from real failure traces
# during the 7th-regression verification — every fragment showed up as
# the agent claiming success after producing zero actual edits.
_GIVEUP_RATIONALISATION_FRAGMENTS: tuple[str, ...] = (
    "did not modify",
    "didn't modify",
    "no app logic",
    "checkpoint commit",
    "checkpoint to avoid",
    "marker commit",
    "i did not change",
    "i didn't change",
    "no actual changes",
    "no changes were made",
    "no edits were made",
    "not modify any",
    "not change any",
    "could not apply",
    "failed to apply",
    "patch could not",
    "without modifying",
    # 9th-regression phrasings — the agent learned new evasions after
    # the initial fragment set went in. Sample summary that slipped
    # past detection: "I have not made any code changes yet — I only
    # read the file so that any upcoming patches can use exact,
    # verbatim context lines. Tell me the specific edit you want me
    # to make..."
    "have not made any",
    "haven't made any",
    "not made any code changes",
    "not made any edits",
    "not made any changes",
    "no code changes yet",
    "no changes yet",
    "i only read",
    "only read the file",
    "tell me the specific edit",
    "tell me what you would like",
    "tell me what changes",
    "tell me what to change",
    "tell me what to add",
    "let me know what you would like",
    "let me know what changes",
    "let me know what to change",
    "what specific edit",
    "what should i change",
    "what should i add",
    "i have not yet",
    "i haven't yet",
    "i did not make",
    "i didn't make",
    "i won't make changes",
    "i will not make changes",
    # 10th-regression phrasings — emerged after the 9th-regression
    # tightening landed. Two distinct evasion shapes:
    #
    # (A) "tell me which file / what to patch" — the agent is asking
    #     the user to disambiguate the task instead of reading the
    #     plan/spec/file tree it already has access to. Surfaced on
    #     `backend.todo.model`, summary excerpt:
    #       "I don't yet know which file you want me to patch. Please
    #        tell me the exact path of the file I should read/modify
    #        ... Once you provide the target path I'll read..."
    #     Existing fragments missed this because none of them keyed on
    #     "tell me the exact path" / "don't know which file".
    "don't yet know which",
    "do not yet know which",
    "don't know which file",
    "do not know which file",
    "tell me the exact path",
    "tell me which file",
    "tell me the file",
    "please tell me the exact",
    "please tell me which",
    "please tell me what file",
    "once you provide the target",
    "once you provide the path",
    "once you confirm the file",
    # (B) plain-English failure admissions — the agent says outright
    #     that its patches didn't land but still summarises the turn.
    #     Surfaced on `backend.todo.crud`, summary excerpt:
    #       "my apply_patch calls failed to land due to hunk
    #        mismatches ... No file changes were made, and the commit
    #        attempt found nothing to commit. I will need to retry..."
    #     The existing "no changes were made" fragment didn't match
    #     because the agent wrote "No file changes were made" — the
    #     extra "file" word broke the substring. Add the variants.
    "failed to land",
    "did not land",
    "didn't land",
    "no file changes",
    "no file edits",
    "hunk mismatch",
    "hunk mismatches",
    "no hunks found",
    "found nothing to commit",
    "nothing to commit",
    "i will need to retry",
    "i'll need to retry",
    "will need to retry with",
    "need to retry with a correctly",
)


def _looks_like_giveup_rationalisation(output: str) -> str | None:
    """Return the first matching giveup-fragment, or None.

    Case-insensitive substring match. Used to override the "intentional
    no-op completion" branch when the agent's summary is actually a
    rationalisation of a failed turn.
    """
    lowered = output.lower()
    for fragment in _GIVEUP_RATIONALISATION_FRAGMENTS:
        if fragment in lowered:
            return fragment
    return None


# Retry prompt for the "agent produced no writes and no summary" case.
# Deliberately concrete: tell the agent *what to do next* rather than
# just "try again". Most silent-giveup cases are an agent that hit one
# apply_patch failure and bailed without re-reading the file.
_NO_WRITE_RETRY_PROMPT = (
    "Attempt {attempt}/{max_attempts} — your previous turn produced no "
    "edits and no summary. That is a giveup, not a completion.\n"
    "\n"
    "**Your next response MUST start with a tool call, not prose.** "
    "Do not narrate what you are about to do; just do it.\n"
    "\n"
    "If `apply_patch` failed, you must:\n"
    "  1. `read_file` the target so your context anchors match the "
    "actual current contents (the file may already contain the "
    "scaffold's existing User/Item classes — your patch context must "
    "include them verbatim).\n"
    "  2. Re-emit the patch with verbatim context.\n"
    "  3. If the file does not yet exist, use `write_file` instead.\n"
    "\n"
    "If you genuinely believe the task is already complete, say so "
    "explicitly in your final reply (one sentence stating *why* the "
    "current code already satisfies the task) and `git_commit` an empty "
    "marker if you haven't yet. Do not return an empty string."
)


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


def _build_agent_error_retry_prompt(
    agent_error: str, attempt: int, max_attempts: int
) -> str:
    """Build a retry prompt for the agent-crash branch.

    Deliberately keeps the raw exception repr OUT of the prompt because
    past Phase-1 traces show the agent copy/pasting things like
    `UnexpectedModelBehavior('Exceeded maximum retries...')` directly
    into a `request_human_review` question — surfacing pydantic-ai
    internals to the user as if it were a meaningful question.

    We classify the failure shape from a substring sniff so the nudge
    can be specific:

    * `UnexpectedModelBehavior` / `Exceeded maximum retries` →
      pydantic-ai exhausted retries on a tool call. The model needs to
      step back and re-read the target file, NOT escalate.
    * `apply_patch` → the patch tool itself raised. Same advice — re-
      read and retry with verbatim context.
    * anything else → generic "try a different approach" nudge.
    """
    err_lower = (agent_error or "").lower()
    if (
        "unexpectedmodelbehavior" in err_lower
        or "exceeded maximum" in err_lower
    ):
        cause = (
            "Your previous attempt exhausted pydantic-ai's per-call retry "
            "budget — almost always because `apply_patch` kept failing on "
            "stale context."
        )
    elif "apply_patch" in err_lower or "patchapplyerror" in err_lower:
        cause = (
            "Your previous attempt failed inside `apply_patch` — the "
            "context anchors didn't match the file's actual contents."
        )
    else:
        cause = "Your previous attempt failed before producing a result."

    return (
        f"Attempt {attempt}/{max_attempts}.\n"
        f"{cause}\n"
        "\n"
        "Recover step by step:\n"
        "  1. `read_file` the target so you can see the *actual current "
        "contents* of the file (it likely already contains the "
        "scaffold's existing User/Item classes — your next patch's "
        "context must include them verbatim).\n"
        "  2. If the file does NOT yet exist, call `write_file` to "
        "create it. `apply_patch` refuses to operate on a non-existent "
        "path.\n"
        "  3. Otherwise, emit a tight `apply_patch` whose @@ context "
        "lines match the current file contents byte-for-byte.\n"
        "\n"
        "Do NOT call `request_human_review` for this — the failure is "
        "stale context, not ambiguity. Only escalate if you re-read the "
        "file, attempted the patch with verbatim context, and the patch "
        "still cannot land for a structural reason you can articulate "
        "(e.g. the spec is internally contradictory)."
    )


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
    agent: Agent[CoderDeps, str],
    prompt: str,
    deps: CoderDeps,
    message_history: list[ModelMessage] | None,
) -> tuple[str, list[ModelMessage], str | None]:
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
    agent: Agent[CoderDeps, str] | None = None,
) -> ValidatorLoopResult:
    """Run one BuildPlan task with automatic validator-feedback retries.

    Args:
        task_prompt:
            The natural-language task description (from the Planner
            Agent, typically something like "Add a `User` Pydantic model
            in backend/app/models/user.py with id/email/created_at").
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
    message_history: list[ModelMessage] | None = None
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
            # Agent crashed; record and retry with a sanitised nudge.
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
            # Next attempt: retry with a *sanitised* nudge. Crucially we
            # do NOT include the raw `agent_error` repr in the prompt —
            # past traces show the agent copy/pasting things like
            # `UnexpectedModelBehavior('Exceeded maximum retries...')`
            # straight into a `request_human_review` question. That is
            # noise to the human and hides the real failure (the agent's
            # patch context didn't match). Instead, use a plain-English
            # nudge keyed on the failure shape.
            current_prompt = _build_agent_error_retry_prompt(
                agent_error, attempt_idx + 1, max_attempts
            )
            continue

        # --- 2. Run validators ---
        # Scope to files the agent touched this run, so pre-existing
        # lint/type debt in unrelated files doesn't masquerade as "this
        # attempt's failures" and derail the model into lint-chasing.
        #
        # If the agent wrote nothing, skip validators entirely. There
        # are two shapes of "no writes" we need to distinguish:
        #
        #   (a) the agent intentionally completed a no-op task and
        #       summarised what it did — common for global ops like
        #       `frontend.routes.register` where TanStack Router
        #       regenerates routeTree.gen.ts on its own. We accept
        #       these and return ok=True; the outer build loop's
        #       commit-SHA check is the authoritative "did anything
        #       change?" signal.
        #
        #   (b) the agent gave up silently — emitted no edits *and* no
        #       meaningful summary. This is the regression we hit during
        #       Phase-1 verification: an agent that hit a single
        #       apply_patch context-mismatch sometimes returned an empty
        #       string, the validator loop swallowed it as ok=True, and
        #       the build "succeeded" without actually doing anything.
        #       We retry with a targeted nudge, or — if this was the last
        #       attempt — fail loudly so the outer loop rolls back and
        #       reports the failure rather than committing a phantom
        #       success.
        if not deps.touched_paths:
            empty_report = ValidatorReport(
                ok=True,
                issue_count=0,
                issues=[],
                commands=[],
            )
            output_stripped = (output or "").strip()
            short_output = len(output_stripped) < _MIN_NO_WRITE_OUTPUT_CHARS
            rationalisation = _looks_like_giveup_rationalisation(output_stripped)
            silent_giveup = short_output or rationalisation is not None
            giveup_reason = (
                "silent giveup (no writes, no summary)"
                if short_output
                else (
                    f"giveup rationalised as success "
                    f"(matched fragment {rationalisation!r})"
                    if rationalisation is not None
                    else None
                )
            )
            attempts.append(
                ValidatorLoopAttempt(
                    attempt=attempt_idx,
                    agent_output=output,
                    agent_turn_count=turn_count,
                    report=empty_report,
                    agent_error=giveup_reason,
                )
            )
            if not silent_giveup:
                log.info(
                    "validator_loop.no_writes_skip_validators",
                    attempt=attempt_idx,
                    turn_count=turn_count,
                    output_chars=len(output_stripped),
                )
                return ValidatorLoopResult(
                    ok=True,
                    attempts_used=attempt_idx,
                    max_attempts=max_attempts,
                    attempts=attempts,
                    final_report=empty_report,
                )

            # Silent giveup branch — retry or fail.
            log.warning(
                "validator_loop.silent_giveup",
                attempt=attempt_idx,
                turn_count=turn_count,
                output_chars=len(output_stripped),
                rationalisation=rationalisation,
            )
            if attempt_idx >= max_attempts:
                # Out of attempts. Surface as a failure so the outer
                # loop rolls back. final_report stays as the empty one
                # so callers can still introspect commands list.
                return ValidatorLoopResult(
                    ok=False,
                    attempts_used=attempt_idx,
                    max_attempts=max_attempts,
                    attempts=attempts,
                    final_report=empty_report,
                )
            # Pick the right retry nudge: a short / empty output gets the
            # generic no-writes prompt; a rationalisation needs an extra
            # line calling out the specific evasion so the agent doesn't
            # repeat it on the next turn.
            if rationalisation is not None:
                current_prompt = (
                    f"Attempt {attempt_idx + 1}/{max_attempts} — your "
                    f"previous turn produced ZERO file edits but your "
                    f"summary claimed success (matched the giveup "
                    f"phrase {rationalisation!r}).\n"
                    "\n"
                    "**Your next response MUST start with a tool call, "
                    "not prose.** Do not explain what you are about to "
                    "do, do not ask clarifying questions — issue the "
                    "tool call. The response shape that ends this "
                    "retry is `read_file` → `apply_patch`/`write_file` "
                    "→ `git_commit`, with no narration in between.\n"
                    "\n"
                    "A `git_commit` with `allow_empty=True` and no "
                    "actual file writes is NOT task completion. Empty "
                    "checkpoint commits do not satisfy a build task — "
                    "the validator suite needs the intended edits to "
                    "exist before it has anything to check.\n"
                    "\n"
                    "Recover step by step:\n"
                    "  1. `read_file` the target so you can see the "
                    "actual current contents.\n"
                    "  2. If `apply_patch` failed because the file "
                    "doesn't exist, switch to `write_file`.\n"
                    "  3. If `apply_patch` failed on context anchors, "
                    "re-emit with verbatim context lines from the read.\n"
                    "  4. Verify your `apply_patch` / `write_file` "
                    "tool result has `ok=True` BEFORE calling "
                    "`git_commit`.\n"
                    "  5. Do NOT call `git_commit(allow_empty=True)` "
                    "to dodge this requirement."
                )
            else:
                current_prompt = _NO_WRITE_RETRY_PROMPT.format(
                    attempt=attempt_idx + 1, max_attempts=max_attempts
                )
            continue

        scope_paths = sorted(deps.touched_paths)
        report = await run_validators(deps, list(targets), paths=scope_paths)

        # Even with non-empty touched_paths the agent can still
        # rationalise a giveup as success — e.g. it `read_file`-d a
        # path then issued a `git_commit("checkpoint", allow_empty=True)`,
        # which doesn't write but does flip touched_paths via a prior
        # successful no-op apply_patch (or, more commonly, leaves
        # touched_paths populated from the failed earlier attempt's
        # message-history-carried state). The 8th-regression trace
        # showed the agent producing a 350-char "I did not modify any
        # app logic" summary while validators returned ok=True
        # (because the only "touched" path was a file it had read but
        # not changed). Treat the summary phrase as authoritative when
        # it appears: the agent told us what it did, take it at its
        # word.
        rationalisation = _looks_like_giveup_rationalisation((output or "").strip())
        attempts.append(
            ValidatorLoopAttempt(
                attempt=attempt_idx,
                agent_output=output,
                agent_turn_count=turn_count,
                report=report,
                agent_error=(
                    f"giveup rationalised as success "
                    f"(matched fragment {rationalisation!r})"
                    if rationalisation is not None
                    else None
                ),
            )
        )

        if report.ok and rationalisation is None:
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

        if report.ok and rationalisation is not None:
            log.warning(
                "validator_loop.giveup_rationalisation_post_validator",
                attempt=attempt_idx,
                turn_count=turn_count,
                rationalisation=rationalisation,
                touched_count=len(deps.touched_paths),
            )
            if attempt_idx >= max_attempts:
                # Out of attempts. Validators passed but the agent's
                # own summary is an admission of non-action. Surface
                # as failure so the outer loop rolls back.
                return ValidatorLoopResult(
                    ok=False,
                    attempts_used=attempt_idx,
                    max_attempts=max_attempts,
                    attempts=attempts,
                    final_report=report,
                )
            current_prompt = (
                f"Attempt {attempt_idx + 1}/{max_attempts} — your "
                f"previous summary admitted non-action (matched the "
                f"giveup phrase {rationalisation!r}) even though the "
                f"validators ran cleanly. The validator pass is "
                f"meaningless if the intended edits were not made.\n"
                "\n"
                "Make the actual changes the task requires this turn. "
                "If you previously committed a checkpoint with no "
                "real edits, that commit does NOT count as task "
                "completion — re-run the planned writes (`apply_patch` "
                "or `write_file`), verify each tool result has "
                "`ok=True`, then `git_commit` with a real summary of "
                "what changed."
            )
            continue

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


def _count_turns(messages: list[ModelMessage]) -> int:
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
