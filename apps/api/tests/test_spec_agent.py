"""Spec Agent tests.

We use `pydantic_ai.models.function.FunctionModel` to ship a deterministic
stand-in for the real LLM. This lets us:

* Exercise the Pydantic AI validation path (returned data must conform to
  AppSpec or the test fails on the *real* schema).
* Assert prompt wiring (system prompt + user prompt shape).
* Run in CI with no Azure creds.

We intentionally do NOT test the real Azure call — that's an integration
smoke test that lives behind a `--integration` pytest marker (Phase 1 wk6).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from alloy_shared.spec import AppSpec
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.agents.spec_agent import build_user_prompt


@dataclass
class _Captured:
    system_prompt: str | None = None
    user_prompt: str | None = None


def _make_valid_spec_response() -> str:
    """Canonical minimal AppSpec JSON the model will return as a tool call
    argument. Pydantic AI parses this into an `AppSpec` instance.
    """
    spec = AppSpec(
        name="Task Tracker",
        slug="task-tracker",
        description="A simple team task tracker.",
        entities=[],
        routes=[],
        pages=[],
        integrations=[],
    )
    return spec.model_dump_json()


def _capturing_function_model(captured: _Captured) -> FunctionModel:
    """A FunctionModel that records what it was sent and returns a valid AppSpec
    encoded as the structured output Pydantic AI expects.
    """

    async def respond(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        # Walk the messages to capture what was sent.
        for msg in messages:
            for part in getattr(msg, "parts", []):
                kind = getattr(part, "part_kind", None)
                if kind == "system-prompt":
                    captured.system_prompt = (captured.system_prompt or "") + part.content
                elif kind == "user-prompt":
                    captured.user_prompt = (captured.user_prompt or "") + (
                        part.content if isinstance(part.content, str) else str(part.content)
                    )

        # For `output_type=AppSpec`, Pydantic AI exposes a "final_result" tool
        # and the model should invoke it with the AppSpec as arguments. Using
        # `FunctionModel` we can short-circuit with a TextPart containing the
        # JSON — Pydantic AI's default TextOutput will validate it.
        return ModelResponse(parts=[TextPart(content=_make_valid_spec_response())])

    return FunctionModel(respond)


def test_build_user_prompt_wraps_with_tags() -> None:
    msg = build_user_prompt("hello", {"what team size?": "3-5"})
    assert "<user_prompt>" in msg
    assert "hello" in msg
    assert "<clarifying_answers>" in msg
    assert "3-5" in msg


def test_build_user_prompt_skips_clarifying_when_empty() -> None:
    msg = build_user_prompt("hello", None)
    assert "<clarifying_answers>" not in msg


@pytest.mark.asyncio
async def test_spec_agent_returns_valid_appspec() -> None:
    captured = _Captured()

    # Construct a lightweight agent; we don't use build_spec_agent() because
    # that reaches for Azure creds at model init time.
    from pathlib import Path

    prompt = (Path(__file__).parent.parent / "app/agents/prompts/spec_agent.md").read_text()

    agent = Agent[None, AppSpec](
        model=_capturing_function_model(captured),
        output_type=AppSpec,
        system_prompt=prompt,
        retries=1,
    )

    user_msg = build_user_prompt("Build me a task tracker for my team.")
    result = await agent.run(user_msg)

    assert isinstance(result.output, AppSpec)
    assert result.output.slug == "task-tracker"
    assert result.output.name == "Task Tracker"

    # Confirm prompt wiring
    assert captured.system_prompt is not None
    assert "Spec Agent" in captured.system_prompt
    assert captured.user_prompt is not None
    assert "task tracker" in captured.user_prompt.lower()


@pytest.mark.asyncio
async def test_spec_agent_respects_output_schema() -> None:
    """If the model returns something not conforming to AppSpec, the agent
    must retry or raise — never silently yield garbage.
    """

    async def bad_respond(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content="{}")])  # missing required fields

    agent = Agent[None, AppSpec](
        model=FunctionModel(bad_respond),
        output_type=AppSpec,
        retries=0,  # fail fast
    )

    with pytest.raises(Exception):  # noqa: B017 — Pydantic AI raises its own types
        await agent.run("anything")
