"""Sandbox manager dependency provider.

The FastAPI build-run endpoint, the `Files` API, and the Coder Agent's
shell-tool surface all need a single shared `LocalSandboxManager` so port
allocation and per-sandbox locks aren't fragmented across requests. This
module exposes a process-wide singleton plus a Block catalogue + base
template path resolver — the three things every build entrypoint needs.

Phase 2 will swap `LocalSandboxManager` out for a Daytona-backed
implementation; the FastAPI dependency `get_sandbox_manager` keeps its
signature so call sites don't change.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import structlog

from app.core.config import settings
from app.sandboxes.manager import LocalSandboxManager, SandboxManager
from app.scaffold import BlockCatalogue, load_catalogue

_log = structlog.get_logger(__name__)


@lru_cache(maxsize=1)
def get_sandbox_manager() -> SandboxManager:
    """Process-wide `SandboxManager` singleton.

    Caching matters: every `LocalSandboxManager` instantiation re-walks the
    workspace root to prime the port allocator and creates a fresh
    `defaultdict[asyncio.Lock]`. Two parallel build-run requests must share
    the same locks or interleaving compose-ups corrupt state.
    """
    workspaces_root = Path(settings.ALLOY_WORKSPACES_ROOT).expanduser().resolve()
    workspaces_root.mkdir(parents=True, exist_ok=True)
    _log.info("sandbox_manager.singleton_init", workspaces_root=str(workspaces_root))
    return LocalSandboxManager(workspaces_root=workspaces_root)


@lru_cache(maxsize=1)
def get_block_catalogue() -> BlockCatalogue:
    """Process-wide block catalogue, loaded from `<repo>/blocks/`.

    Cached so we don't re-parse all `block.yaml` files on every build. The
    catalogue is read-only at runtime; reload is a process restart away.
    """
    blocks_root = _repo_root() / "blocks"
    return load_catalogue(blocks_root)


@lru_cache(maxsize=1)
def get_base_template_dir() -> Path:
    """Path to the canonical base template (`templates/base-react-fastapi/`).

    Resolved relative to the repo root so `apps/api` can move without
    breaking the scaffold path.
    """
    return _repo_root() / "templates" / "base-react-fastapi"


def _repo_root() -> Path:
    """Walk up from this file to find the monorepo root.

    Strategy: this file lives at `<repo>/apps/api/app/sandboxes/dependency.py`
    so four `parent` calls land on the repo root. We don't trust env
    variables for this — the path must be deterministic from the source
    location so worker processes started from arbitrary cwds still resolve
    correctly.
    """
    return Path(__file__).resolve().parents[4]


__all__ = [
    "get_base_template_dir",
    "get_block_catalogue",
    "get_sandbox_manager",
]
