"""Tests do WorkerPool — Semaphore + counters."""
from __future__ import annotations

import asyncio

import pytest

from ai_company.worker_pool import WorkerPool


async def test_acquire_release_counts():
    pool = WorkerPool(size=3)
    assert pool.size == 3
    assert pool.in_use == 0
    assert pool.free == 3

    async with pool.acquire("t1"):
        assert pool.in_use == 1
        assert pool.free == 2
        async with pool.acquire("t2"):
            assert pool.in_use == 2
            assert pool.free == 1
        assert pool.in_use == 1

    assert pool.in_use == 0
    assert pool.free == 3


async def test_blocks_when_full():
    pool = WorkerPool(size=1)
    order: list[str] = []

    async def acquire(label: str, hold_time: float):
        async with pool.acquire(label):
            order.append(f"in:{label}")
            await asyncio.sleep(hold_time)
            order.append(f"out:{label}")

    # Kicks off 2 topics, pool=1 should serialize
    t1 = asyncio.create_task(acquire("a", 0.05))
    await asyncio.sleep(0.005)  # ensures a grabs the slot first
    t2 = asyncio.create_task(acquire("b", 0.05))
    await asyncio.gather(t1, t2)

    assert order == ["in:a", "out:a", "in:b", "out:b"]
    assert pool.in_use == 0


async def test_invalid_size():
    with pytest.raises(ValueError):
        WorkerPool(size=0)
    with pytest.raises(ValueError):
        WorkerPool(size=-1)
