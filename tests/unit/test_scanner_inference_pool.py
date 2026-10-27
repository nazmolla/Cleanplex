"""Detection runs on a pool sized to the scan workers (issue #103)."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

import cleanplex.scanner as scanner


@pytest.fixture(autouse=True)
def no_pool_leak():
    yield
    scanner._replace_inference_pool(None)


def _which_thread(_):
    time.sleep(0.01)  # long enough that idle threads would be picked up if allowed
    return threading.get_ident()


async def test_inference_uses_at_most_scan_workers_threads():
    """Regression: the default pool's 16 threads each loaded its own model copy."""
    scanner._replace_inference_pool(2)

    threads = await asyncio.gather(*(scanner._infer(_which_thread, i) for i in range(40)))

    assert len(set(threads)) <= 2


async def test_replacing_the_pool_starts_fresh_threads_and_stops_the_old_ones():
    scanner._replace_inference_pool(1)
    first = await scanner._infer(_which_thread, 0)
    old_pool = scanner._inference_pool

    scanner._replace_inference_pool(1)
    second = await scanner._infer(_which_thread, 0)

    assert old_pool._shutdown is True
    assert first != second


async def test_without_a_pool_inference_still_runs():
    scanner._replace_inference_pool(None)

    assert isinstance(await scanner._infer(_which_thread, 0), int)
