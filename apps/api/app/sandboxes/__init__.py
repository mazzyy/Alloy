"""Sandbox manager — per-project workspaces with docker-compose lifecycle.

This is Phase 1 wk3-4's stand-in for Daytona Cloud. The interface is
designed so the Daytona implementation drops in behind `SandboxManager`
without the callers (Coder Agent, Validator loop, preview-URL routes)
knowing.

Everything in this package is async. Disk / subprocess IO goes via
`asyncio.create_subprocess_exec` — never the blocking `subprocess.run`.
"""

from __future__ import annotations

from app.sandboxes.manager import (
    LocalSandboxManager,
    SandboxManager,
)
from app.sandboxes.ports import FixedPortAllocator, PortAllocator, ProbingPortAllocator
from app.sandboxes.runtime import (
    ContainerRuntime,
    DockerComposeRuntime,
    ExecResult,
    FakeContainerRuntime,
)
from app.sandboxes.types import SandboxError, SandboxHandle, SandboxInfo, SandboxStatus

__all__ = [
    "ContainerRuntime",
    "DockerComposeRuntime",
    "ExecResult",
    "FakeContainerRuntime",
    "FixedPortAllocator",
    "LocalSandboxManager",
    "PortAllocator",
    "ProbingPortAllocator",
    "SandboxError",
    "SandboxHandle",
    "SandboxInfo",
    "SandboxManager",
    "SandboxStatus",
]
