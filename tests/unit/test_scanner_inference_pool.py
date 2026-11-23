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


async def test_idle_pool_is_replaced_so_its_models_are_freed():
    """#220: models live in the pool's threads, so an idle pool must be swapped out."""
    scanner._replace_inference_pool(1)
    await scanner._infer(_which_thread, 0)
    old_pool = scanner._inference_pool
    assert not scanner.release_idle_models(now=time.monotonic())  # just used
    assert scanner.release_idle_models(now=time.monotonic() + scanner.MODEL_IDLE_RELEASE_S + 1)
    assert scanner._inference_pool is not old_pool
    assert scanner._inference_pool._max_workers == 1
    # A fresh pool holds nothing, so it is not released again until it is used.
    assert not scanner.release_idle_models(now=time.monotonic() + 10 * scanner.MODEL_IDLE_RELEASE_S)


async def test_pool_is_not_released_while_inference_is_running():
    scanner._replace_inference_pool(1)
    started = threading.Event()

    def slow(_):
        started.set()
        time.sleep(0.2)

    task = asyncio.create_task(scanner._infer(slow, 0))
    await asyncio.to_thread(started.wait)
    old_pool = scanner._inference_pool
    assert not scanner.release_idle_models(now=time.monotonic() + 10 * scanner.MODEL_IDLE_RELEASE_S)
    assert scanner._inference_pool is old_pool
    await task


async def test_unused_pool_is_never_released():
    scanner._replace_inference_pool(1)
    assert not scanner.release_idle_models(now=time.monotonic() + 10 * scanner.MODEL_IDLE_RELEASE_S)
