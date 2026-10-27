"""Idle scanner workers wake as soon as work arrives (issue #87)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import time as dtime

import pytest

import cleanplex.scanner as scanner
from cleanplex import database as db
from cleanplex.config import Config

pytestmark = pytest.mark.usefixtures("setup_db")


@pytest.fixture(autouse=True)
def fresh_queues(monkeypatch):
    # asyncio primitives bind to the loop that first waits on them; each test runs
    # on its own loop, so give every test fresh module state.
    monkeypatch.setattr(scanner, "_scan_queue", asyncio.Queue())
    monkeypatch.setattr(scanner, "_force_scan_queue", asyncio.Queue())
    monkeypatch.setattr(scanner, "_queue_cond", asyncio.Condition())
    monkeypatch.setattr(scanner, "_state_lock", asyncio.Lock())
    for name in ("_queued_normal", "_queued_force", "_current_guids"):
        monkeypatch.setattr(scanner, name, set())
    for name in ("_queued_force_ordered", "_queued_normal_ordered"):
        monkeypatch.setattr(scanner, name, [])
    monkeypatch.setattr(scanner, "_paused", False)


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
