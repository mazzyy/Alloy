"""Planner Agent tests — focus on the deterministic block resolver plus a
structured-output smoke test via FunctionModel.
"""

from __future__ import annotations

import pytest
from alloy_shared.plan import BuildPlan, FileOp, FileOpKind
from alloy_shared.spec import AppSpec, AuthConfig, AuthProvider, Integration
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.agents.planner_agent import resolve_blocks_for_spec


def _base_spec(**overrides: object) -> AppSpec:
    defaults: dict[str, object] = {
        "name": "Task Tracker",
        "slug": "task-tracker",
        "description": "x",
    }
    defaults.update(overrides)
    return AppSpec.model_validate(defaults)


def test_resolve_blocks_default_clerk() -> None:
    assert resolve_blocks_for_spec(_base_spec()) == ["auth/clerk"]


def test_resolve_blocks_non_clerk_auth_skips_clerk_block() -> None:
    spec = _base_spec(auth=AuthConfig(provider=AuthProvider.custom_jwt))
    assert resolve_blocks_for_spec(spec) == []


def test_resolve_blocks_picks_integrations() -> None:
    spec = _base_spec(
        integrations=[Integration(kind="r2"), Integration(kind="stripe"), Integration(kind="clerk")]
    )
    got = resolve_blocks_for_spec(spec)
    # clerk shows up from both auth default and the explicit integration —
    # the set dedups; sorted output gives a stable assertion.
    assert got == ["auth/clerk", "billing/stripe-subscriptions", "storage/r2"]


def test_resolve_blocks_unknown_integration_is_ignored() -> None:
    # daytona has no Alloy block yet; we simply don't map it.
    spec = _base_spec(integrations=[Integration(kind="daytona")])
    assert resolve_blocks_for_spec(spec) == ["auth/clerk"]


@pytest.mark.asyncio
async def test_planner_agent_returns_valid_buildplan() -> None:
    """Round-trip: a FunctionModel returns a BuildPlan JSON, the Agent
    parses it, and we get a typed BuildPlan back out.
    """
    expected_plan = BuildPlan(
        spec_slug="task-tracker",
        blocks=["auth/clerk"],
        ops=[
            FileOp(
                id="backend.openapi_export",
                kind=FileOpKind.create,
                path="apps/api/openapi.json",
                intent="export OpenAPI schema",
                depends_on=[],
            ),
        ],
    )

    async def respond(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content=expected_plan.model_dump_json())])

    agent = Agent[None, BuildPlan](
        model=FunctionModel(respond),
        output_type=BuildPlan,
        retries=0,
    )
    result = await agent.run("dummy")
    assert isinstance(result.output, BuildPlan)
    assert result.output.spec_slug == "task-tracker"
    assert result.output.blocks == ["auth/clerk"]
    assert result.output.ops[0].id == "backend.openapi_export"
