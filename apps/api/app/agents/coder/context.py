"""Agent dependency object — `CoderDeps`.

Pydantic AI's `Agent[DepsT, OutputT]` passes a typed `deps` object into
every tool call via `RunContext[DepsT]`. We use it to plumb:

* The sandbox workspace root (every path tool validates against it)
* An optional `SandboxManager` handle for tools that need to `exec` inside
  the container (validators, openapi export, alembic)
* A shared `structlog` logger so tool calls carry turn/project IDs
* A retry budget — how many more times a tool can fail before the outer
  loop should bail this task

Kept frozen so tests can diff expected-vs-actual deps without fighting
mutation aliasing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog


@dataclass
class CoderDeps:
    """Runtime context every Coder Agent tool needs.

    `sandbox` is kept `Any`-typed to avoid a hard import cycle with
    `app.sandboxes.manager` — the agent doesn't care whether it's a real
    `LocalSandboxManager` or a fake, only that it has an async `.exec()`.
    """

    workspace_root: Path
    # Optional so tests can exercise pure-filesystem tools without
    # instantiating a SandboxManager.
    sandbox: Any | None = None
    # Optional handle; required when `sandbox` is set.
    sandbox_handle: Any | None = None
    # Correlation IDs propagated into structured logs and Langfuse traces.
    turn_id: str | None = None
    project_id: str | None = None
    # How many tool failures this task has left before we bail.
    retry_budget: int = 3
    # structlog's `get_logger` returns a BoundLoggerLazyProxy that
    # proxies every method onto a real BoundLogger on first use, so
    # this is `Any`-typed to sidestep mypy's strict view of the proxy.
    logger: Any = field(
        default_factory=lambda: structlog.get_logger("alloy.coder_agent"),
    )

    def __post_init__(self) -> None:
        self.workspace_root = Path(self.workspace_root).expanduser().resolve()
        if not self.workspace_root.exists() or not self.workspace_root.is_dir():
            # Raise here (at deps-construction time) so a broken workspace
            # fails fast, before the agent starts burning tokens.
            raise ValueError(
                f"CoderDeps.workspace_root must be an existing directory: {self.workspace_root}"
            )

    def bind(self, **extra: Any) -> Any:
        """Return a logger bound with the standard correlation fields."""
        bindings: dict[str, Any] = {}
        if self.turn_id:
            bindings["turn_id"] = self.turn_id
        if self.project_id:
            bindings["project_id"] = self.project_id
        bindings.update(extra)
        return self.logger.bind(**bindings)
