"""Unit tests for bg_jobs.py — stale job recovery and job status helper."""

from __future__ import annotations

import json

import pytest

from cleanplex import database as db
from cleanplex.bg_jobs import recover_stale_jobs, get_job_status


pytestmark = pytest.mark.usefixtures("setup_db")


# ── recover_stale_jobs ─────────────────────────────────────────────────────────

async def test_recover_stale_jobs_marks_running_as_failed():
    job_id = await db.create_bg_job("upload")
    # create_bg_job sets status='running'
    await recover_stale_jobs()
    job = await db.get_bg_job(job_id)
    assert job["status"] == "failed"
    assert "restarted" in job["error_message"].lower()


async def test_recover_stale_jobs_marks_queued_as_failed():
    # Insert a 'queued' job directly to test that path
    async with db.get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO bg_jobs(job_type, status) VALUES (?, ?)",
            ("upload", "queued"),
        )
        await conn.commit()
        job_id = cursor.lastrowid

    await recover_stale_jobs()
    job = await db.get_bg_job(job_id)
    assert job["status"] == "failed"


async def test_recover_stale_jobs_leaves_completed_unchanged():
    job_id = await db.create_bg_job("upload")
    await db.update_bg_job(job_id, status="completed", progress=100)
    await recover_stale_jobs()
    job = await db.get_bg_job(job_id)
    # completed jobs must remain completed
    assert job["status"] == "completed"


async def test_recover_stale_jobs_leaves_failed_unchanged():
    job_id = await db.create_bg_job("upload")
    await db.update_bg_job(job_id, status="failed", error="some error")
    await recover_stale_jobs()
    job = await db.get_bg_job(job_id)
    assert job["status"] == "failed"
    assert job["error_message"] == "some error"


async def test_recover_stale_jobs_no_stale_jobs_is_no_op():
    # Ensure it runs without error when there's nothing to recover
    await recover_stale_jobs()  # should not raise


# ── get_job_status ─────────────────────────────────────────────────────────────

async def test_get_job_status_returns_none_for_missing():
    result = await get_job_status(99999)
    assert result is None


async def test_get_job_status_returns_dict():
    job_id = await db.create_bg_job("upload")
    status = await get_job_status(job_id)
    assert status is not None
    assert status["id"] == job_id
    assert status["job_type"] == "upload"
    assert status["status"] == "running"


async def test_get_job_status_parses_result_data():
    job_id = await db.create_bg_job("upload")
    result_payload = {"status": "success", "files_processed": 5}
    await db.update_bg_job(job_id, result=json.dumps(result_payload))
    status = await get_job_status(job_id)
    assert status["result"] == result_payload


async def test_get_job_status_result_none_when_no_result():
    job_id = await db.create_bg_job("upload")
    status = await get_job_status(job_id)
    assert status["result"] is None


async def test_get_job_status_includes_progress():
    job_id = await db.create_bg_job("upload")
    await db.update_bg_job(job_id, progress=60)
    status = await get_job_status(job_id)
    assert status["progress"] == 60
