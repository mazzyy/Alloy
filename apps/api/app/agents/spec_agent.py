"""Spec Agent — user prompt → AppSpec.

Roadmap §3 Phase 1: "Intake Agent asks clarifying questions via structured
JSON, then a Spec Agent using Pydantic AI emits a validated `AppSpec` object."

This file ships **the Spec Agent proper** — a single Pydantic AI `Agent` that
produces a typed `AppSpec` from a user prompt. The Intake/clarifying-question
loop is a thin wrapper added in Phase 1 wk4; the Spec Agent itself already
handles optional clarifying answers as additional context.

Key design choices:

1. **Typed output via `output_type=AppSpec`**. Pydantic AI generates an
   OpenAI tool schema from the Pydantic model and validates the returned
   JSON. Retries are bounded by `retries=2` on the Agent.
2. **System prompt is loaded from `prompts/spec_agent.md`** so we can iterate
   on it without a redeploy and eventually version it in Langfuse.
3. **Streaming is caller-controlled** — the route uses `agent.run_stream()`
   to surface tokens for live UX; the unit tests use `agent.run()` for a
   blocking result.
4. We register **no tools** on the Spec Agent. The only thing it can do is
   emit an `AppSpec`. Tool use is reserved for the Coder Agent (Phase 1 wk5).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from alloy_shared.spec import AppSpec
from pydantic_ai import Agent

from app.agents.models import default_settings, get_planner_model

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def build_spec_agent() -> Agent[None, AppSpec]:
    """Return the singleton Spec Agent.

    Cached so we don't rebuild on every request. Tests should call
    `build_spec_agent.cache_clear()` if they need a fresh agent with a
    different model override.
    """
    return Agent[None, AppSpec](
        model=get_planner_model(),
        output_type=AppSpec,
        system_prompt=_load_prompt("spec_agent.md"),
        model_settings=default_settings(reasoning_effort="low", max_output_tokens=4000),
        retries=2,
        name="alloy.spec_agent",
    )


# A second agent we can construct lazily for testing without Azure creds.
# The route handler normally uses `build_spec_agent()`; tests can construct
# a lightweight `Agent` directly and override with `TestModel`.


def build_user_prompt(
    prompt: str,
    clarifying_answers: dict[str, str] | None = None,
) -> str:
    """Format the user's raw prompt plus any clarifying Q&A into a single
    message for the Spec Agent.

    We keep this out of the Agent itself so the route can show the user
    exactly what we sent (audit + replay).
    """
    parts = [f"<user_prompt>\n{prompt.strip()}\n</user_prompt>"]
    if clarifying_answers:
        qa_lines = "\n".join(f"- **{q}**: {a}" for q, a in clarifying_answers.items())
        parts.append(f"<clarifying_answers>\n{qa_lines}\n</clarifying_answers>")
    return "\n\n".join(parts)
