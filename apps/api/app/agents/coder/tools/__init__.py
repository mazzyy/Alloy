"""Coder Agent tool registry.

`register_tools(agent)` wires every tool module onto a Pydantic AI `Agent`.
Kept split from `agent.py` so tests can build a trimmed agent with just a
subset of tools (e.g. the patch-only test agent).

Each tool module exports a top-level `register(agent)` function that
registers its tools. Order doesn't matter for correctness but we keep the
sequence in `ALL_TOOL_MODULES` stable so the system-prompt tool listing
reads the same every time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.agents.coder.tools import (
    codegen,
    commands,
    files,
    git,
    patch,
    review,
    search,
    validators,
)

if TYPE_CHECKING:
    from pydantic_ai import Agent

    from app.agents.coder.context import CoderDeps


# Stable order: read-only → write → exec → meta. Matches how we teach the
# agent to think in the system prompt.
ALL_TOOL_MODULES = (
    files,
    search,
    patch,
    commands,
    validators,
    codegen,
    git,
    review,
)


def register_tools(agent: "Agent[CoderDeps, str]") -> None:
    """Register every Coder Agent tool on `agent`.

    Pydantic AI's `@agent.tool` decorator attaches handlers to a specific
    agent instance. We centralise registration here so `build_coder_agent`
    stays short and tests can stub this function to register a subset.
    """
    for module in ALL_TOOL_MODULES:
        module.register(agent)


__all__ = ["ALL_TOOL_MODULES", "register_tools"]
