"""Port allocator tests."""

from __future__ import annotations

import socket

import pytest

from app.sandboxes.ports import FixedPortAllocator, ProbingPortAllocator


async def test_probing_allocator_rejects_invalid_range():
    with pytest.raises(ValueError):
        ProbingPortAllocator(start=100, end=100)
    with pytest.raises(ValueError):
        ProbingPortAllocator(start=80, end=8080)  # privileged range


async def test_probing_allocator_finds_free_port():
    alloc = ProbingPortAllocator(start=40000, end=40100)
    port = await alloc.allocate()
    assert 40000 <= port < 40100


async def test_probing_allocator_skips_claimed_and_reserved():
    alloc = ProbingPortAllocator(start=40100, end=40200)
    first = await alloc.allocate()
    # Next allocation must not be the same
    second = await alloc.allocate()
    assert second != first
    # Reserved takes precedence over probing
    third = await alloc.allocate(reserved={second + 1, second + 2})
    assert third not in {first, second, second + 1, second + 2}


async def test_probing_allocator_skips_actually_busy_port():
    alloc = ProbingPortAllocator(start=40200, end=40210)
    # Hold the first port in the range
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 40200))
    s.listen(1)
    try:
        port = await alloc.allocate()
        assert port != 40200
    finally:
        s.close()


async def test_probing_allocator_free_allows_reallocation():
    alloc = ProbingPortAllocator(start=40300, end=40320)
    p = await alloc.allocate()
    await alloc.free(p)
    # After freeing, the same port may be re-handed-out on a later call
    # (we don't strictly require it, but the claimed-set should no
    # longer block it).
    assert p not in alloc._claimed  # noqa: SLF001 — internal check


async def test_probing_allocator_prime_persists_claims():
    alloc = ProbingPortAllocator(start=40400, end=40410)
    alloc.prime({40401, 40402})
    # Now allocating should skip those two.
    seen: set[int] = set()
    for _ in range(3):
        seen.add(await alloc.allocate())
    assert 40401 not in seen
    assert 40402 not in seen


async def test_fixed_allocator_hands_out_in_order():
    alloc = FixedPortAllocator([30001, 30002, 30003])
    assert await alloc.allocate() == 30001
    assert await alloc.allocate() == 30002
    assert await alloc.allocate() == 30003
    with pytest.raises(RuntimeError, match="exhausted"):
        await alloc.allocate()


async def test_fixed_allocator_respects_reserved():
    alloc = FixedPortAllocator([30001, 30002, 30003])
    got = await alloc.allocate(reserved={30001})
    assert got == 30002
