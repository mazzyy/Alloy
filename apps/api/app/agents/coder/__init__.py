"""Coder Agent — the workhorse that reads, writes, patches, and validates code
inside a sandbox workspace.

Public surface:

* `build_coder_agent()` — cached Pydantic AI `Agent` with every tool registered
* `CoderDeps` — the dependency object the agent carries (sandbox handle,
  logger, retry budget)
* `CoderAgentError` — any coder-agent-specific failure surfaces as this
* Tool result types (`FileRead`, `PatchResult`, `CommandResult`, ...) — also
  exported from `app.agents.coder.results` so call sites outside the agent
  can assert on them in tests

We intentionally split tools into small modules under `tools/` so the test
for `apply_patch` can exercise just the patch logic without dragging in an
LLM model, a sandbox, or pydantic-ai.
"""

from __future__ import annotations

from app.agents.coder.agent import build_coder_agent
from app.agents.coder.context import CoderDeps
from app.agents.coder.errors import CoderAgentError
from app.agents.coder.results import (
    AlembicResult,
    AstSummary,
    CommandResult,
    FileList,
    FileRead,
    GitCommitResult,
    HumanReviewRequested,
    PatchResult,
    SearchHits,
    ValidatorLoopAttempt,
    ValidatorLoopResult,
    ValidatorReport,
    WriteResult,
)
from app.agents.coder.validator_loop import run_task_with_validators

__all__ = [
    "AlembicResult",
    "AstSummary",
    "CoderAgentError",
    "CoderDeps",
    "CommandResult",
    "FileList",
    "FileRead",
    "GitCommitResult",
    "HumanReviewRequested",
    "PatchResult",
    "SearchHits",
    "ValidatorLoopAttempt",
    "ValidatorLoopResult",
    "ValidatorReport",
    "WriteResult",
    "build_coder_agent",
    "run_task_with_validators",
]
