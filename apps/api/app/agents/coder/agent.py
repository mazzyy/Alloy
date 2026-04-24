"""Coder Agent — the Pydantic AI agent with the full tool schema wired up.

Public entry point: `build_coder_agent()` returns a cached
`Agent[CoderDeps, str]`. Tests should call
`build_coder_agent.cache_clear()` if they need a fresh agent with a
different model override.

Output is a plain string — the agent's English summary of what it did
this turn. Structured state (file diffs, commit SHAs, validator
reports) already lives on the sandbox filesystem + git log by the time
the final string is produced, so we don't also try to return it in
typed form.

`retries=1` because pydantic-ai's built-in retry is for schema
validation of tool args — it's separate from our own retry budget for
validator-loop failures (`CoderDeps.retry_budget`). One schema retry
is plenty; anything more means the LLM is genuinely confused and
should get a fresh turn instead.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_ai import Agent

from app.agents.coder.context import CoderDeps
from app.agents.coder.tools import register_tools
from app.agents.models import default_settings, get_planner_model

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def _load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def build_coder_agent() -> Agent[CoderDeps, str]:
    """Return the singleton Coder Agent."""
    agent = Agent[CoderDeps, str](
        model=get_planner_model(),
        deps_type=CoderDeps,
        output_type=str,
        system_prompt=_load_prompt("coder_agent.md"),
        # Higher token cap than the Spec Agent — the coder emits
        # actual code + patch bodies, not just a typed spec. 8K keeps
        # us under the 128K output cap while leaving headroom for
        # reasoning tokens.
        model_settings=default_settings(reasoning_effort="low", max_output_tokens=8000),
        # `retries=3` is pydantic-ai's per-tool retry budget (separate
        # from our validator-loop retry budget). We bumped from 1 → 3
        # after an Azure run where the model emitted a malformed
        # apply_patch call, got the ModelRetry hint, emitted *another*
        # malformed call with path="" (different bug, also retry-able),
        # and then pydantic-ai gave up with UnexpectedModelBehavior —
        # consuming an entire validator-loop attempt. Three per-turn
        # retries is enough for the model to try a write_file fallback,
        # re-read the file, or correct path-arg mistakes before we bail
        # the whole attempt. Still small enough that a truly stuck agent
        # (wrong mental model of a file) surfaces quickly.
        retries=3,
        name="alloy.coder_agent",
    )
    register_tools(agent)
    return agent
