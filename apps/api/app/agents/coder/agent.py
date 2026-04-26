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

`retries=6` is pydantic-ai's per-tool retry budget — separate from
our own retry budget for validator-loop failures
(`CoderDeps.retry_budget`). It catches transient tool-arg schema
problems and `apply_patch` stale-context misses where the next
ModelRetry payload (which embeds a file excerpt) is enough for the
model to recover. See the inline comment on the `retries=` argument
in `build_coder_agent` for the full history of why this number is
six and not one or three.
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
        # `retries=6` is pydantic-ai's per-tool retry budget (separate
        # from our validator-loop retry budget).
        #
        # History:
        #   1 → 3: Azure run where the model emitted a malformed
        #     apply_patch, got the ModelRetry hint, emitted *another*
        #     malformed call with path="", then pydantic-ai gave up
        #     with UnexpectedModelBehavior, consuming an entire
        #     validator-loop attempt.
        #   3 → 6: `backend.note.model` task on the
        #     fastapi-fullstack-template scaffold. The agent kept
        #     calling apply_patch with stale @@ context (the file
        #     already contained scaffold User/Item classes the agent
        #     hadn't read yet). Each retry embedded a file excerpt in
        #     the ModelRetry payload, but it took the model 3+ tries
        #     to align its context with the actual contents — at which
        #     point pydantic-ai had already exhausted the budget and
        #     surfaced UnexpectedModelBehavior, blowing the whole
        #     validator-loop attempt despite the model being on track.
        #
        # Six is still well under "infinite retry" — a truly stuck
        # agent (wrong mental model of a file, incorrect target path)
        # surfaces inside ~30s — but gives the model real headroom to
        # converge on stale-context cases that have a deterministic fix
        # (re-read + verbatim re-paste).
        retries=6,
        name="alloy.coder_agent",
    )
    register_tools(agent)
    return agent
