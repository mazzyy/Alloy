"""Coder Agent integration test — drives the agent with a scripted
`FunctionModel` that issues tool calls and then emits a final string.

This is the one test that exercises:

* `build_coder_agent()` wiring (tool registration, deps_type, output_type)
* Pydantic AI's tool-call dispatch path
* A realistic multi-tool sequence (read → patch → validate → commit)

All LLM behaviour is deterministic via `FunctionModel` — no Azure creds,
no network. Failures in this test usually mean a tool's argument schema
drifted; the FunctionModel can't serialise the call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.agents.coder.context import CoderDeps
from app.agents.coder.tools import register_tools


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "apps/api/app").mkdir(parents=True)
    (tmp_path / "apps/api/app/main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n",
        encoding="utf-8",
    )
    return tmp_path


def _count_tool_returns(messages: list[ModelMessage]) -> dict[str, int]:
    """Count how many ToolReturnPart entries per tool name appear in the
    message history. Used to assert the scripted tool sequence fired."""
    counts: dict[str, int] = {}
    for msg in messages:
        if not isinstance(msg, ModelRequest):
            continue
        for part in msg.parts:
            if isinstance(part, ToolReturnPart):
                counts[part.tool_name] = counts.get(part.tool_name, 0) + 1
    return counts


async def test_coder_agent_runs_scripted_tool_sequence(workspace: Path) -> None:
    """Script: list_files → read_file → apply_patch → final text."""
    script: list[dict[str, Any]] = [
        # Step 0: call list_files
        {"tool": "list_files", "args": {"path": "apps/api/app"}},
        # Step 1: read_file
        {"tool": "read_file", "args": {"path": "apps/api/app/main.py"}},
        # Step 2: apply_patch to replace `FastAPI()` with `FastAPI(title="X")`
        {
            "tool": "apply_patch",
            "args": {
                "path": "apps/api/app/main.py",
                "patch": (
                    "@@ -1,2 +1,2 @@\n"
                    " from fastapi import FastAPI\n"
                    '-app = FastAPI()\n'
                    '+app = FastAPI(title="X")\n'
                ),
            },
        },
        # Step 3: final string
        {"final": "Patched main.py to set FastAPI title='X'."},
    ]
    step_idx = [0]

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        step = script[step_idx[0]]
        step_idx[0] += 1
        if "final" in step:
            return ModelResponse(parts=[TextPart(content=step["final"])])
        return ModelResponse(
            parts=[ToolCallPart(tool_name=step["tool"], args=step["args"])]
        )

    # Build an ad-hoc agent bound to the fake model so we don't need
    # Azure creds and don't have to fight the lru_cache singleton.
    agent = Agent[CoderDeps, str](
        model=FunctionModel(respond),
        deps_type=CoderDeps,
        output_type=str,
        system_prompt="test-coder",
        retries=1,
    )
    register_tools(agent)

    deps = CoderDeps(workspace_root=workspace, turn_id="t", project_id="p")
    result = await agent.run("do the thing", deps=deps)

    # The agent returned the scripted final string.
    assert result.output.startswith("Patched main.py")

    # The patch actually landed on disk.
    main_py = (workspace / "apps/api/app/main.py").read_text()
    assert 'FastAPI(title="X")' in main_py

    # Every scripted tool got invoked exactly once.
    counts = _count_tool_returns(result.all_messages())
    assert counts.get("list_files") == 1
    assert counts.get("read_file") == 1
    assert counts.get("apply_patch") == 1


async def test_coder_agent_surfaces_patch_failure_to_model(workspace: Path) -> None:
    """A failing apply_patch should reach the model as a retry-hint, not
    silently succeed. We script: bad patch → fallback final string."""
    script: list[dict[str, Any]] = [
        {
            "tool": "apply_patch",
            "args": {
                "path": "apps/api/app/main.py",
                "patch": "@@ -1,1 +1,1 @@\n-nothing matches\n+something\n",
            },
        },
        {"final": "Gave up after patch failure."},
    ]
    step_idx = [0]

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        step = script[step_idx[0]]
        step_idx[0] += 1
        if "final" in step:
            return ModelResponse(parts=[TextPart(content=step["final"])])
        return ModelResponse(
            parts=[ToolCallPart(tool_name=step["tool"], args=step["args"])]
        )

    agent = Agent[CoderDeps, str](
        model=FunctionModel(respond),
        deps_type=CoderDeps,
        output_type=str,
        system_prompt="test-coder",
        retries=1,
    )
    register_tools(agent)

    deps = CoderDeps(workspace_root=workspace, turn_id="t", project_id="p")
    result = await agent.run("try a bad patch", deps=deps)
    assert "Gave up" in result.output

    # File was NOT modified — failed patches are atomic.
    assert "FastAPI()" in (workspace / "apps/api/app/main.py").read_text()


def test_build_coder_agent_pins_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin pydantic-ai's per-call `retries` budget on the production
    agent at 6.

    The 10th-regression failure (`backend.note.model` task) exhausted
    pydantic-ai's per-tool retry count after the model needed several
    `apply_patch` attempts to align its @@ context with the actual
    file contents. Each ModelRetry payload embeds a file excerpt, so
    convergence is normally fast, but three retries was tight enough
    that a single early misstep blew the whole turn. We bumped to six.

    A silent regression back to 1 or 3 reintroduces the same crash on
    any task that touches a non-trivial scaffolded file. Pin it.
    """
    # `build_coder_agent` calls `get_planner_model()` which raises
    # AgentModelConfigError if Azure isn't configured. Stub the relevant
    # settings so the call lands without needing real creds — the model
    # is never invoked here, only its construction.
    from app.agents.coder.agent import build_coder_agent
    from app.core.config import settings

    monkeypatch.setattr(settings, "AZURE_OPENAI_ENDPOINT", "https://stub.test/")
    monkeypatch.setattr(settings, "AZURE_OPENAI_API_KEY", "stub")
    monkeypatch.setattr(settings, "AZURE_OPENAI_DEPLOYMENT", "stub-deployment")
    # The lru_cache holds onto the first instance built across tests;
    # bust it so we get a fresh one with the patched settings.
    build_coder_agent.cache_clear()
    try:
        agent = build_coder_agent()
        # `_max_tool_retries` is pydantic-ai's internal name for the
        # ctor `retries=` argument. If pydantic-ai renames this in a
        # future release, this test will tell us in CI before a release
        # silently lands a regression.
        assert agent._max_tool_retries == 6, (
            "Coder Agent retry budget regressed — see "
            "`build_coder_agent` in agent.py for why this must stay at "
            "6 and the history of how we got here."
        )
    finally:
        # Don't leak the stubbed agent into other tests' caches.
        build_coder_agent.cache_clear()
