"""Port allocation for sandboxes.

Every sandbox needs two host-published TCP ports: one for the frontend
(vite preview / nginx), one for the backend FastAPI. We allocate them
from a configurable range (`SANDBOX_PORT_RANGE_START..END`).

Two implementations:

* `ProbingPortAllocator` — single-process, probes via `socket.bind`. Good
  for local dev and tests. Thread-safe via asyncio lock.
* `FixedPortAllocator` — test harness that hands out a preset queue.

In production we'll grow a `RedisPortAllocator` backed by a Redis SETNX
range so multiple API replicas don't double-allocate. That's a drop-in
for the `PortAllocator` protocol when we need it.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Protocol


class PortAllocator(Protocol):
    """Interface the manager depends on — keep surface minimal."""

    async def allocate(self, reserved: set[int] | None = None) -> int: ...
    async def free(self, port: int) -> None: ...


def _is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """Does `host:port` accept a bind right now?

    Caveat: this is TOCTOU. Between our probe and Docker actually binding
    something else could grab the port. Mitigation: we bind to
    `127.0.0.1` (not `0.0.0.0`) and Docker binds the same, so the race
    window is small enough to tolerate for local dev.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
    except OSError:
        return False
    finally:
        s.close()
    return True


class ProbingPortAllocator:
    """Scan a range for free ports, with an in-memory claimed-set.

    The claimed-set survives only for this process; on restart we rely
    on the sandbox state files (`.alloy/sandbox.json`) to reconstruct
    which ports are in use — see `LocalSandboxManager._load_state`.
    """

    def __init__(self, start: int = 20000, end: int = 29999) -> None:
        if start >= end:
            raise ValueError(f"start ({start}) must be < end ({end})")
        if start < 1024:
            raise ValueError("refuse to allocate from privileged range (<1024)")
        self._start = start
        self._end = end
        self._claimed: set[int] = set()
        self._lock = asyncio.Lock()

    async def allocate(self, reserved: set[int] | None = None) -> int:
        async with self._lock:
            taken = self._claimed | (reserved or set())
            for p in range(self._start, self._end):
                if p in taken:
                    continue
                if _is_port_free(p):
                    self._claimed.add(p)
                    return p
            raise RuntimeError(
                f"No free port in [{self._start}, {self._end}); {len(self._claimed)} claimed"
            )

    async def free(self, port: int) -> None:
        async with self._lock:
            self._claimed.discard(port)

    def prime(self, ports: set[int]) -> None:
        """Mark ports as claimed without probing — used on startup to
        rehydrate from persisted sandbox state files.

        Synchronous on purpose: only called during manager construction.
        """
        self._claimed |= ports


class FixedPortAllocator:
    """Test allocator: returns ports from a preset queue in order.

    Raises if the queue is exhausted. Tests should supply enough
    elements for the whole scenario.
    """

    def __init__(self, ports: list[int]) -> None:
        self._remaining: list[int] = list(ports)
        self._lock = asyncio.Lock()

    async def allocate(self, reserved: set[int] | None = None) -> int:
        async with self._lock:
            reserved = reserved or set()
            while self._remaining:
                p = self._remaining.pop(0)
                if p in reserved:
                    continue
                return p
            raise RuntimeError("FixedPortAllocator exhausted")

    async def free(self, port: int) -> None:
        # Tests should not depend on port recycling — no-op.
        return None
