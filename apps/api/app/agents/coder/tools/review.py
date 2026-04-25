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
_GENERIC_QUESTION_FRAGMENTS: tuple[str, ...] = (
    "what should i do",
    "what would you like me to do",
    "what specific file or change",
    "what file or change",
    "please specify",
    "please clarify",
    "should i (re)attempt",
    "should i reattempt",
    "what do you want me to",
    "give me exact instructions",
    "provide the exact task",
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
                "If you escalated because `apply_patch` failed with a "
                "context mismatch, do NOT escalate — call `read_file` on "
                "the target, look at the actual contents, and re-emit "
                "the patch with verbatim context. Most patch failures are "
                "stale anchors, not real ambiguities."
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
