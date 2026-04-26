"""`request_human_review` — the agent's escape hatch for ambiguous specs
or destructive migrations.

Raises `HumanReviewRequired` so the LangGraph outer loop catches it and
persists the question. The tool also returns `HumanReviewRequested` so
the Langfuse trace records the question *as a tool return*, not as an
uncaught exception — the raise happens right after, and pydantic-ai's
trace captures both events.

Quality guardrails on the question:
    The whole reason this tool exists is that *the model* knows something
    it can't resolve and *the human* needs to weigh in. If the model
    submits a generic "what should I do now?" question, neither party
    benefits — the human has no context, the model burns a build pause,
    and the conversation regresses. So we reject:

      - empty / whitespace-only questions
      - questions shorter than `_MIN_QUESTION_LEN` characters
      - questions that match common "I'm stuck, please tell me anything"
        templates (case-insensitive substring match against
        `_GENERIC_QUESTION_FRAGMENTS`)

    Rejection comes back to the model as `ModelRetry` so it gets one more
    chance to either ask a real question or — better — go retry the
    underlying tool call (re-read the file, fix the patch).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai import ModelRetry, RunContext

from app.agents.coder.context import CoderDeps
from app.agents.coder.errors import HumanReviewRequired, ToolInputError
from app.agents.coder.results import HumanReviewRequested

if TYPE_CHECKING:
    from pydantic_ai import Agent


# Below this many chars, a question is by definition non-actionable. 40
# chars is "Should I delete the User table?" — short but specific.
# Anything shorter is almost always a content-free escalation.
_MIN_QUESTION_LEN = 40


# Substrings that signal a content-free escalation. Case-insensitive
# match. Curated from real failure traces during Phase-1 verification —
# every one of these patterns showed up as a regression where the agent
# escalated on a first patch failure instead of re-reading the file.
#
# The list is union-of-symptoms, not orthogonal — overlapping fragments
# are fine, the goal is to catch the model's evasions across rephrases.
_GENERIC_QUESTION_FRAGMENTS: tuple[str, ...] = (
    # "what should I … ?" family
    "what should i do",
    "what should i edit",
    "what should i change",
    "what should i write",
    "what should i (re)attempt",
    "what should i reattempt",
    # "what would you like / want me to … ?" family
    "what would you like me to do",
    "what would you like me to edit",
    "what do you want me to",
    # "what (specific) file/change/code … ?" family
    "what specific file or change",
    "what file or change",
    "which file and change",
    "which file or change",
    "what file should i",
    "which file should i",
    # "please specify / clarify / describe / provide" family
    "please specify",
    "please clarify",
    "please describe",
    "please provide",
    # "give me / provide … instructions/task" family
    "give me exact instructions",
    "give me instructions",
    "provide the exact task",
    "provide exact instructions",
    "provide instructions",
    # "I need clarification / I'm uncertain" family
    "i need clarification",
    "i am uncertain how to proceed",
    "i'm uncertain how to proceed",
    # Generic "should I retry?" without specifics
    "should i (re)attempt",
    "should i reattempt",
    "should i retry",
    # Agent leaking pydantic-ai internals into its question
    "unexpectedmodelbehavior",
    "exceeded maximum retries",
    # 11th-regression family — the agent escalates a *patch mechanic*
    # question that it should answer itself by calling write_file.
    # Surfaced on `backend.todo.migration`, question excerpt:
    #   "apply_patch keeps failing with 'no hunks found' — my patches
    #    are very small edits but the tool won't accept them. ...
    #    Options: 1) Allow me to overwrite the file with write_file
    #            2) Advise a specific exact hunk lines"
    # The right answer is option 1, which the agent already has — its
    # tool schema includes `write_file` and the system prompt says to
    # use it as a fallback. Asking the human to pick is a no-op
    # escalation; reject so the agent commits to write_file itself.
    "apply_patch keeps failing",
    "apply_patch will not accept",
    "apply_patch won't accept",
    "patch won't accept",
    "no hunks found",
    "tool won't accept them",
    "tool will not accept them",
    "allow me to overwrite",
    "may i use write_file",
    "may i overwrite",
    "should i use write_file",
    "should i overwrite the file",
    "advise a specific exact hunk",
    "advise the exact hunk",
    "advise the hunk",
    "provide the exact lines",
    "provide the exact hunk",
    # 12th-regression family — the agent escalates an alembic /
    # tooling diagnostic question framed as A/B options when the real
    # answer is "read the stderr you already have". Surfaced on
    # `backend.todo.migration`, question excerpt:
    #   "alembic_autogenerate returned ok=false and produced no
    #    migration file ... I can proceed with two options: (A) update
    #    env.py ... or (B) you can allow me to run alembic directly in
    #    verbose mode to get more diagnostic output."
    # The right answer is to read `result.stderr` — which the tool
    # now surfaces — not to guess at env.py imports or ask for a
    # bash escape hatch. Reject so the agent is forced to use the
    # diagnostic info it already has.
    "fails silently",
    "fail silently",
    "ok=false and produced no",
    "returned ok=false and",
    "run alembic directly in verbose",
    "alembic in verbose mode",
    "allow me to run alembic",
    "more diagnostic output",
    "more diagnostic info",
    "to get diagnostics",
)


def _looks_generic(question: str) -> str | None:
    """Return a human-readable reason if `question` is a generic
    placeholder, else None.
    """
    stripped = question.strip()
    if len(stripped) < _MIN_QUESTION_LEN:
        return (
            f"question is too short ({len(stripped)} chars; need "
            f"≥{_MIN_QUESTION_LEN}). Include what you tried, the exact "
            f"error, and concrete options the human can choose between."
        )
    lowered = stripped.lower()
    for fragment in _GENERIC_QUESTION_FRAGMENTS:
        if fragment in lowered:
            return (
                f"question contains the generic placeholder "
                f"{fragment!r}. Replace it with a specific question that "
                f"includes (1) what you tried, (2) the exact tool error, "
                f"and (3) concrete options the human can pick between."
            )
    return None


def register(agent: Agent[CoderDeps, str]) -> None:
    @agent.tool
    async def request_human_review(
        ctx: RunContext[CoderDeps],
        question: str,
        options: list[str] | None = None,
    ) -> HumanReviewRequested:
        """Pause and ask the human for a decision.

        Use this when:
        * The AppSpec is ambiguous in a way you can't resolve by reading
          existing code.
        * A migration contains destructive operations (drop_table, etc.).
        * A validator's diagnostics point to an architectural choice
          (e.g. renaming a public route contract) rather than a typo.

        Do **not** use this for situations you can resolve yourself —
        notably, do not escalate on the first `apply_patch` context-
        mismatch failure. Re-read the file and retry with verbatim
        context first. See the system prompt's "When (not) to escalate"
        section for the full rule set.

        The LangGraph outer loop pauses the run and surfaces `question`
        (with `options` as quick-reply buttons) in the UI. Every human
        review blocks the build, so the question must be specific:
        include what you tried, the exact tool error, and concrete
        options the human can pick between.
        """
        if not question or not question.strip():
            raise ToolInputError("review question must be non-empty")

        rejection = _looks_generic(question)
        if rejection is not None:
            ctx.deps.bind(
                tool="request_human_review",
                rejected=True,
                question=question,
            ).warning("coder.human_review.rejected_generic")
            # ModelRetry feeds back to the model as a tool-error so it
            # gets one more turn to either fix its question or — much
            # better — go retry the underlying tool call. We keep the
            # original question in the message so the model can see why
            # we rejected it without us having to re-explain.
            raise ModelRetry(
                f"request_human_review rejected: {rejection}\n"
                f"Your question was: {question!r}\n\n"
                "If you escalated because `apply_patch` failed (context "
                "mismatch, no hunks found, the tool 'won't accept' your "
                "patch, etc.), do NOT escalate. You have two equivalent "
                "fallbacks and you must pick one yourself:\n"
                "  (a) `read_file` the target, then re-emit `apply_patch` "
                "with verbatim context lines.\n"
                "  (b) `write_file(..., overwrite=True)` with the full "
                "intended file contents — this is always allowed and is "
                "the correct fallback when your patch body is small or "
                "the anchors are short. Read the file first so you don't "
                "clobber unrelated existing code.\n"
                "Pick whichever lands the change fastest. Asking the "
                "human to choose between (a) and (b) is itself a giveup.\n"
                "\n"
                "If you escalated because `alembic_autogenerate` "
                "returned `ok=false` and you don't know why: the tool "
                "result already contains `stderr` and `returncode` "
                "fields. Read `result.stderr` — alembic prints almost "
                "every error there (config issues, missing tables, "
                "import failures). Do NOT ask the human for a 'verbose "
                "mode' or guess at env.py imports; the diagnostic you "
                "need is already in your tool result."
            )

        opts = list(options or [])
        ctx.deps.bind(tool="request_human_review", question=question, options=opts).warning(
            "coder.human_review"
        )
        # Raising signals the LangGraph outer loop to pause the run.
        # We pre-construct the result so any trace middleware peeking
        # at the argument-to-be-returned has a clean record of the
        # question, then raise to halt the agent.
        _ = HumanReviewRequested(question=question, options=opts)
        raise HumanReviewRequired(question=question, options=opts)
