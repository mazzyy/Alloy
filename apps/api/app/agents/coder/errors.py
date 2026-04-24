"""Coder Agent error hierarchy.

We keep these isolated from `SandboxError` (lifecycle faults) and
`AgentModelConfigError` (Azure creds missing). Tool handlers convert
low-level failures into `ToolResultError` so the LLM sees a structured
error it can respond to, rather than a raw traceback.

Why structured errors matter: pydantic-ai's default behavior on tool
exception is to forward the exception string back to the model as a
retry prompt. That works for "file not found", but for patch failures
we want the model to see *which hunks matched and which didn't* so it
can decide whether to retry with a different anchor or give up and
call `write_file` instead.
"""

from __future__ import annotations


class CoderAgentError(RuntimeError):
    """Base class for Coder Agent failures surfaced to the caller."""


class WorkspaceEscapeError(CoderAgentError):
    """A tool was asked to touch a path outside the workspace root.

    Always a programmer/LLM error — never surface to the model as a
    retryable condition; raise it so the outer loop can bail.
    """


class DisallowedCommandError(CoderAgentError):
    """`run_command` received a binary not on the allow-list."""


class ToolInputError(CoderAgentError):
    """A tool was called with malformed or unsafe arguments.

    Bubbles back to the LLM as a retry hint — e.g. "line range is
    inverted" or "patch is empty". We want the model to correct itself,
    not for the whole turn to die.
    """


class PatchApplyError(CoderAgentError):
    """`apply_patch` couldn't land a patch cleanly.

    Carries `.details` — a list of per-hunk outcomes — so the LLM can
    see which hunks matched, which didn't, and (for failed hunks) what
    nearby context we saw instead.
    """

    def __init__(self, message: str, *, details: list[dict[str, object]] | None = None) -> None:
        super().__init__(message)
        self.details = details or []


class HumanReviewRequired(CoderAgentError):
    """Agent called `request_human_review` — the outer loop must pause.

    This is *not* a tool *failure*; it's a signal to the LangGraph outer
    loop that the agent has deferred to a human. The loop catches this,
    persists the question, and surfaces it in the UI.
    """

    def __init__(self, question: str, options: list[str] | None = None) -> None:
        super().__init__(question)
        self.question = question
        self.options = options or []
