"""Idle scanner workers wake as soon as work arrives (issue #87)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import time as dtime

import pytest
from unittest.mock import AsyncMock

import cleanplex.scanner as scanner
from cleanplex import database as db
from cleanplex.config import Config

pytestmark = pytest.mark.usefixtures("setup_db")


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    # asyncio primitives bind to the loop that first waits on them; each test runs
    # on its own loop, so give every test fresh module state.
    monkeypatch.setattr(scanner, "_queue_cond", asyncio.Condition())
    monkeypatch.setattr(scanner, "_state_lock", asyncio.Lock())
    monkeypatch.setattr(scanner, "_current_guids", set())
    monkeypatch.setattr(scanner, "_paused", False)
    # These tests are about the wake-up path; the idle self-heal would queue the pending
    # jobs by itself before the producer acts.
    monkeypatch.setattr(scanner, "sync_queue", AsyncMock(return_value=0))


async def _run_workers(monkeypatch, n, in_window, action):
    started: dict[str, float] = {}

    async def fake_scan(guid, config):
        started[guid] = time.monotonic()
        await asyncio.sleep(3600)

    monkeypatch.setattr(scanner, "scan_video", fake_scan)
    # A window that is always open, or one that never is.
    window = (dtime(0, 0), dtime(23, 59)) if in_window else (dtime(3, 0), dtime(3, 0))
    config = replace(Config(), scan_window_start=window[0], scan_window_end=window[1])
    if not in_window:
        monkeypatch.setattr(Config, "is_scan_window", lambda self: False)

    async def get_config():
        return config

    for i in range(n):
        await db.upsert_scan_job(plex_guid=f"g{i}", title=f"T{i}", file_path=f"/{i}", rating_key=str(i),
                                 library_id="lib", library_title="Lib")
    workers = [asyncio.create_task(scanner._scanner_worker_loop(i + 1, get_config)) for i in range(n)]
    await asyncio.sleep(0.2)  # let every worker go idle first
    t0 = time.monotonic()
    await action(n)
    await asyncio.sleep(1.0)
    for w in workers:
        w.cancel()
    await asyncio.gather(*workers, return_exceptions=True)
    return {g: t - t0 for g, t in started.items()}


async def test_force_scans_start_every_idle_worker_at_once(monkeypatch):
    async def force_all(n):
        for i in range(n):
            await scanner.force_scan_job(f"g{i}")

    started = await _run_workers(monkeypatch, 4, True, force_all)

    assert sorted(started) == ["g0", "g1", "g2", "g3"]
    assert max(started.values()) < 0.5


async def test_normal_enqueue_wakes_an_idle_worker(monkeypatch):
    async def enqueue_one(n):
        await scanner.enqueue("g0")

    started = await _run_workers(monkeypatch, 2, True, enqueue_one)

    assert list(started) == ["g0"] and started["g0"] < 0.5


async def test_outside_window_only_force_scans_start(monkeypatch):
    async def mixed(n):
        await scanner.enqueue("g0")
        await scanner.force_scan_job("g1")

    started = await _run_workers(monkeypatch, 2, False, mixed)

    assert list(started) == ["g1"] and started["g1"] < 0.5


async def test_a_worker_claims_a_queued_job_from_the_database(monkeypatch):
    claimed = []

    async def fake_scan(guid, config):
        claimed.append(guid)
        await asyncio.sleep(3600)

    monkeypatch.setattr(scanner, "scan_video", fake_scan)
    config = replace(Config(), scan_window_start=dtime(0, 0), scan_window_end=dtime(23, 59))

    async def get_config():
        return config

    await db.upsert_scan_job(plex_guid="q1", title="Q", file_path="/q", rating_key="1", library_id="l", library_title="L")
    await db.queue_scan_jobs(["q1"])
    worker = asyncio.create_task(scanner._scanner_worker_loop(1, get_config))
    await asyncio.sleep(0.3)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)

    assert claimed == ["q1"]
    assert (await db.get_scan_job_by_guid("q1"))["status"] == "scanning"


async def test_an_idle_worker_asks_the_database_for_unqueued_pending_titles(monkeypatch):
    monkeypatch.setattr(scanner, "sync_queue", AsyncMock(return_value=0))
    config = replace(Config(), scan_window_start=dtime(0, 0), scan_window_end=dtime(23, 59))

    async def get_config():
        return config

    worker = asyncio.create_task(scanner._scanner_worker_loop(1, get_config))
    await asyncio.sleep(0.3)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    scanner.sync_queue.assert_awaited()
