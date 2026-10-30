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
