"""Leaving a scan early closes the frame generator at once instead of leaving it for shutdown (#159)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import cleanplex.scanner as scanner
from cleanplex import database as db
from cleanplex.config import Config
from tests.unit.test_scanner_refine import _job

pytestmark = pytest.mark.usefixtures("setup_db")


async def test_a_skipped_scan_closes_the_frame_generator_before_returning(tmp_path):
    await _job(tmp_path)
    closed: list[bool] = []

    async def batch(path, step_ms, duration_ms, size, hardware=False):
        try:
            for t in range(0, duration_ms + 1, step_ms):
                yield t, f"frame-{t}".encode()
                scanner._skip_requested_guids.add("g")  # the user asks to skip once scanning has begun
        finally:
            closed.append(True)

    config = Config(scan_step_ms=2000, siglip_enabled=False)
    with patch.object(scanner, "extract_frames_batch", batch), \
         patch.object(scanner, "get_duration_ms", AsyncMock(return_value=60_000)), \
         patch.object(scanner, "_classify_frame", lambda *a, **k: (False, 0.0, [])):
        await scanner.scan_video("g", config)

    assert closed == [True]
    assert (await db.get_scan_job_by_guid("g"))["status"] == "pending"


# ── a released scan goes back to the database queue (#174) ────────────────────

async def test_a_skipped_scan_goes_to_the_back_of_the_queue(tmp_path):
    await _job(tmp_path)
    await db.set_force_scan("g", False)  # the helper forces it; this test is about the normal queue
    await db.upsert_scan_job("other", "Other", "/o.mkv", "2", "lib", "L")
    await db.queue_scan_jobs(["g", "other"])

    async def batch(path, step_ms, duration_ms, size, hardware=False):
        scanner._skip_requested_guids.add("g")
        yield 0, b"frame"

    with patch.object(scanner, "extract_frames_batch", batch), \
         patch.object(scanner, "get_duration_ms", AsyncMock(return_value=60_000)), \
         patch.object(scanner, "_classify_frame", lambda *a, **k: (False, 0.0, [])):
        await scanner.scan_video("g", Config(scan_step_ms=2000, siglip_enabled=False))

    assert [j["plex_guid"] for j in await db.get_queued_scan_jobs()] == ["other", "g"]


async def test_a_cancelled_scan_is_handed_back_instead_of_staying_scanning(tmp_path):
    import asyncio
    await _job(tmp_path)

    async def batch(path, step_ms, duration_ms, size, hardware=False):
        yield 0, b"frame"
        await asyncio.sleep(3600)

    with patch.object(scanner, "extract_frames_batch", batch), \
         patch.object(scanner, "get_duration_ms", AsyncMock(return_value=60_000)), \
         patch.object(scanner, "_classify_frame", lambda *a, **k: (False, 0.0, [])):
        task = asyncio.create_task(scanner.scan_video("g", Config(scan_step_ms=2000, siglip_enabled=False)))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    job = await db.get_scan_job_by_guid("g")
    assert job["status"] == "pending"
    assert [j["plex_guid"] for j in await db.get_queued_scan_jobs()] == ["g"]


async def test_an_ignored_title_claimed_by_mistake_leaves_the_queue(tmp_path):
    await _job(tmp_path)
    await db.set_force_scan("g", False)  # a forced title bypasses the ignore gate
    await db.queue_scan_jobs(["g"])
    await db.claim_next_scan_job()
    await db.set_ignored("g", True)  # ignored after the claim, before the scan starts

    await scanner.scan_video("g", Config())

    job = await db.get_scan_job_by_guid("g")
    assert job["status"] == "pending"
    assert await db.count_queued_scan_jobs() == 0


async def test_a_forced_scan_keeps_running_while_the_scanner_is_paused(tmp_path, monkeypatch):
    await _job(tmp_path)  # the helper forces the job
    monkeypatch.setattr(scanner, "_paused", True)  # an idle worker paused the scanner (#201)
    frames: list[int] = []

    async def batch(path, step_ms, duration_ms, size, hardware=False):
        for t in range(0, 6000, step_ms):
            yield t, b"frame"

    def classify(jpeg, *a, **k):
        frames.append(1)
        return False, 0.0, []

    with patch.object(scanner, "extract_frames_batch", batch), \
         patch.object(scanner, "get_duration_ms", AsyncMock(return_value=6_000)), \
         patch.object(scanner, "_classify_frame", classify):
        await scanner.scan_video("g", Config(scan_step_ms=2000, siglip_enabled=False))

    assert len(frames) == 3
    assert (await db.get_scan_job_by_guid("g"))["status"] == "done"


async def test_a_normal_scan_stops_and_requeues_while_the_scanner_is_paused(tmp_path, monkeypatch):
    await _job(tmp_path)
    await db.set_force_scan("g", False)
    monkeypatch.setattr(scanner, "_paused", True)

    async def batch(path, step_ms, duration_ms, size, hardware=False):
        yield 0, b"frame"

    with patch.object(scanner, "extract_frames_batch", batch), \
         patch.object(scanner, "get_duration_ms", AsyncMock(return_value=60_000)), \
         patch.object(scanner, "_classify_frame", lambda *a, **k: (False, 0.0, [])):
        await scanner.scan_video("g", Config(scan_step_ms=2000, siglip_enabled=False))

    assert (await db.get_scan_job_by_guid("g"))["status"] == "pending"
