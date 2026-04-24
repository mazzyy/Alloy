"""`request_human_review` — the agent's escape hatch for ambiguous specs
or destructive migrations.

Raises `HumanReviewRequired` so the LangGraph outer loop catches it and
persists the question. The tool also returns `HumanReviewRequested` so
the Langfuse trace records the question *as a tool return*, not as an
uncaught exception — the raise happens right after, and pydantic-ai's
trace captures both events.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai import RunContext

from app.agents.coder.context import CoderDeps
from app.agents.coder.errors import HumanReviewRequired, ToolInputError
from app.agents.coder.results import HumanReviewRequested

if TYPE_CHECKING:
    from pydantic_ai import Agent


def register(agent: "Agent[CoderDeps, str]") -> None:
    @agent.tool
    async def request_human_review(
        ctx: "RunContext[CoderDeps]",
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

        The LangGraph outer loop pauses the run and surfaces `question`
        (with `options` as quick-reply buttons) in the UI. Do not call
        this for questions you could answer by reading one more file —
        every human review blocks the build.
        """
        if not question or not question.strip():
            raise ToolInputError("review question must be non-empty")
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
