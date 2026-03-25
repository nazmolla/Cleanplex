"""Unit tests for database.py — all queries run against a real in-memory SQLite file."""

from __future__ import annotations

import pytest

from cleanplex import database as db


pytestmark = pytest.mark.usefixtures("setup_db")


# ── Settings ───────────────────────────────────────────────────────────────────

async def test_get_setting_returns_default():
    val = await db.get_setting("plex_url")
    assert val == ""


async def test_set_and_get_setting():
    await db.set_setting("plex_url", "http://localhost:32400")
    assert await db.get_setting("plex_url") == "http://localhost:32400"


async def test_set_setting_overwrites_existing():
    await db.set_setting("plex_url", "http://first")
    await db.set_setting("plex_url", "http://second")
    assert await db.get_setting("plex_url") == "http://second"


async def test_get_setting_returns_fallback_for_missing_key():
    result = await db.get_setting("nonexistent_key", default="fallback")
    assert result == "fallback"


async def test_update_settings_writes_multiple_keys():
    await db.update_settings({"plex_url": "http://a", "plex_token": "tok123"})
    assert await db.get_setting("plex_url") == "http://a"
    assert await db.get_setting("plex_token") == "tok123"


async def test_get_all_settings_returns_dict():
    settings = await db.get_all_settings()
    assert isinstance(settings, dict)
    # init_db seeds DEFAULT_SETTINGS
    assert "plex_url" in settings
    assert "confidence_threshold" in settings


# ── User Filters ───────────────────────────────────────────────────────────────

async def test_upsert_and_get_user_filter():
    await db.upsert_user_filter("alice", enabled=True)
    row = await db.get_user_filter("alice")
    assert row is not None
    assert row["plex_username"] == "alice"
    assert row["enabled"] == 1


async def test_get_user_filter_returns_none_for_missing():
    result = await db.get_user_filter("nobody")
    assert result is None


async def test_upsert_user_filter_disables():
    await db.upsert_user_filter("bob", enabled=True)
    await db.upsert_user_filter("bob", enabled=False)
    row = await db.get_user_filter("bob")
    assert row["enabled"] == 0


async def test_get_all_user_filters_returns_list():
    await db.upsert_user_filter("alice", enabled=True)
    await db.upsert_user_filter("bob", enabled=False)
    filters = await db.get_all_user_filters()
    usernames = [f["plex_username"] for f in filters]
    assert "alice" in usernames
    assert "bob" in usernames


# ── Segments ───────────────────────────────────────────────────────────────────

async def test_insert_and_get_segments_for_guid():
    seg_id = await db.insert_segment(
        plex_guid="guid-1",
        title="Test Movie",
        start_ms=1000,
        end_ms=5000,
        confidence=0.9,
        labels="NUDITY",
    )
    assert seg_id > 0

    rows = await db.get_segments_for_guid("guid-1")
    assert len(rows) == 1
    assert rows[0]["start_ms"] == 1000
    assert rows[0]["end_ms"] == 5000
    assert rows[0]["labels"] == "NUDITY"


async def test_get_segments_for_guid_returns_empty_for_unknown():
    rows = await db.get_segments_for_guid("unknown-guid")
    assert rows == []


async def test_get_segments_ordered_by_start_ms():
    await db.insert_segment("guid-ord", "T", start_ms=5000, end_ms=6000)
    await db.insert_segment("guid-ord", "T", start_ms=1000, end_ms=2000)
    rows = await db.get_segments_for_guid("guid-ord")
    assert rows[0]["start_ms"] == 1000
    assert rows[1]["start_ms"] == 5000


async def test_count_segments_for_guid():
    await db.insert_segment("guid-cnt", "T", start_ms=0, end_ms=1000)
    await db.insert_segment("guid-cnt", "T", start_ms=2000, end_ms=3000)
    count = await db.count_segments_for_guid("guid-cnt")
    assert count == 2


async def test_count_segments_for_guid_returns_zero_when_none():
    count = await db.count_segments_for_guid("no-such-guid")
    assert count == 0


async def test_delete_segment_removes_row():
    seg_id = await db.insert_segment("guid-del", "T", start_ms=0, end_ms=1000)
    deleted = await db.delete_segment(seg_id)
    assert deleted is True
    rows = await db.get_segments_for_guid("guid-del")
    assert rows == []


async def test_delete_segment_returns_false_for_missing():
    deleted = await db.delete_segment(99999)
    assert deleted is False


async def test_delete_segments_for_guid_clears_all():
    await db.insert_segment("guid-bulk", "T", start_ms=0, end_ms=1000)
    await db.insert_segment("guid-bulk", "T", start_ms=2000, end_ms=3000)
    count = await db.delete_segments_for_guid("guid-bulk")
    assert count == 2
    assert await db.get_segments_for_guid("guid-bulk") == []


async def test_get_segment_by_id():
    seg_id = await db.insert_segment("guid-id", "T", start_ms=100, end_ms=200, confidence=0.75)
    row = await db.get_segment_by_id(seg_id)
    assert row is not None
    assert row["confidence"] == pytest.approx(0.75)


async def test_get_segment_by_id_returns_none_for_missing():
    row = await db.get_segment_by_id(99999)
    assert row is None


async def test_get_segments_grouped_by_title():
    await db.insert_segment("guid-grp-1", "Movie A", start_ms=0, end_ms=1000)
    await db.insert_segment("guid-grp-2", "Movie B", start_ms=0, end_ms=1000)
    await db.insert_segment("guid-grp-2", "Movie B", start_ms=2000, end_ms=3000)
    groups = await db.get_segments_grouped_by_title()
    counts = {g["plex_guid"]: g["segment_count"] for g in groups}
    assert counts["guid-grp-1"] == 1
    assert counts["guid-grp-2"] == 2


# ── Scan Jobs ──────────────────────────────────────────────────────────────────

async def _make_job(guid: str = "guid-job", title: str = "Movie", status: str = "pending"):
    await db.upsert_scan_job(
        plex_guid=guid,
        title=title,
        file_path=f"/media/{guid}.mkv",
        rating_key="100",
        library_id="1",
        library_title="Movies",
    )
    if status != "pending":
        await db.update_scan_job_status(guid, status)


async def test_upsert_scan_job_creates_job():
    await _make_job()
    job = await db.get_scan_job_by_guid("guid-job")
    assert job is not None
    assert job["title"] == "Movie"
    assert job["status"] == "pending"


async def test_upsert_scan_job_is_idempotent():
    await _make_job()
    await _make_job()  # second call with same guid should be a no-op (INSERT OR IGNORE)
    jobs = await db.get_scan_jobs()
    guids = [j["plex_guid"] for j in jobs if j["plex_guid"] == "guid-job"]
    assert len(guids) == 1


async def test_get_scan_job_by_guid_returns_none_for_missing():
    result = await db.get_scan_job_by_guid("nonexistent")
    assert result is None


async def test_update_scan_job_status_scanning():
    await _make_job("guid-scan")
    await db.update_scan_job_status("guid-scan", "scanning", progress=0.4)
    job = await db.get_scan_job_by_guid("guid-scan")
    assert job["status"] == "scanning"
    assert job["progress"] == pytest.approx(0.4)
    assert job["started_at"] is not None


async def test_update_scan_job_status_done():
    await _make_job("guid-done")
    await db.update_scan_job_status("guid-done", "done", progress=1.0)
    job = await db.get_scan_job_by_guid("guid-done")
    assert job["status"] == "done"
    assert job["finished_at"] is not None


async def test_update_scan_job_status_failed_with_error():
    await _make_job("guid-fail")
    await db.update_scan_job_status("guid-fail", "failed", error_msg="ffmpeg died")
    job = await db.get_scan_job_by_guid("guid-fail")
    assert job["status"] == "failed"
    assert job["error_msg"] == "ffmpeg died"


async def test_reset_scan_job():
    await _make_job("guid-reset")
    await db.update_scan_job_status("guid-reset", "done", progress=1.0)
    await db.reset_scan_job("guid-reset")
    job = await db.get_scan_job_by_guid("guid-reset")
    assert job["status"] == "pending"
    assert job["progress"] == 0


async def test_set_force_scan():
    await _make_job("guid-force")
    await db.set_force_scan("guid-force", True)
    job = await db.get_scan_job_by_guid("guid-force")
    assert job["force_scan"] == 1


async def test_set_ignored():
    await _make_job("guid-ignore")
    await db.set_ignored("guid-ignore", True)
    job = await db.get_scan_job_by_guid("guid-ignore")
    assert job["ignored"] == 1


async def test_get_scan_jobs_by_guids_returns_dict():
    await _make_job("guid-batch-1", "Title A")
    await _make_job("guid-batch-2", "Title B")
    result = await db.get_scan_jobs_by_guids(["guid-batch-1", "guid-batch-2"])
    assert set(result.keys()) == {"guid-batch-1", "guid-batch-2"}
    assert result["guid-batch-1"]["title"] == "Title A"


async def test_get_scan_jobs_by_guids_empty_input_returns_empty():
    result = await db.get_scan_jobs_by_guids([])
    assert result == {}


async def test_get_scan_jobs_by_guids_ignores_missing():
    await _make_job("guid-exists")
    result = await db.get_scan_jobs_by_guids(["guid-exists", "guid-missing"])
    assert "guid-exists" in result
    assert "guid-missing" not in result


async def test_get_scan_jobs_by_library():
    await db.upsert_scan_job(
        plex_guid="lib-guid-1",
        title="Film",
        file_path="/f.mkv",
        rating_key="1",
        library_id="lib99",
        library_title="Cinema",
    )
    jobs = await db.get_scan_jobs_by_library("lib99")
    assert len(jobs) == 1
    assert jobs[0]["plex_guid"] == "lib-guid-1"


async def test_get_segment_counts_for_library():
    await db.upsert_scan_job(
        plex_guid="seg-count-guid",
        title="X",
        file_path="/x.mkv",
        rating_key="2",
        library_id="lib-seg",
        library_title="L",
    )
    await db.insert_segment("seg-count-guid", "X", start_ms=0, end_ms=1000)
    await db.insert_segment("seg-count-guid", "X", start_ms=2000, end_ms=3000)
    counts = await db.get_segment_counts_for_library("lib-seg")
    assert counts.get("seg-count-guid") == 2


# ── get_segments_by_rating_key ─────────────────────────────────────────────────

async def test_get_segments_by_rating_key():
    await db.upsert_scan_job(
        plex_guid="rk-guid",
        title="R",
        file_path="/r.mkv",
        rating_key="rk42",
        library_id="1",
        library_title="L",
    )
    await db.insert_segment("rk-guid", "R", start_ms=0, end_ms=1000)
    rows = await db.get_segments_by_rating_key("rk42")
    assert len(rows) == 1
    assert rows[0]["plex_guid"] == "rk-guid"


async def test_get_segments_by_rating_key_returns_empty_for_unknown():
    rows = await db.get_segments_by_rating_key("no-such-rk")
    assert rows == []


# ── get_local_library_for_sync ─────────────────────────────────────────────────

async def test_get_local_library_for_sync_groups_by_guid():
    await db.upsert_scan_job(
        plex_guid="sync-guid",
        title="Sync Movie",
        file_path="/sync.mkv",
        rating_key="99",
        library_id="1",
        library_title="L",
    )
    await db.update_scan_job_status("sync-guid", "done")
    await db.insert_segment("sync-guid", "Sync Movie", start_ms=0, end_ms=1000)
    await db.insert_segment("sync-guid", "Sync Movie", start_ms=2000, end_ms=3000)

    result = await db.get_local_library_for_sync()
    guids = [r["plex_guid"] for r in result]
    assert "sync-guid" in guids
    item = next(r for r in result if r["plex_guid"] == "sync-guid")
    assert item["segments_count"] == 2
    assert len(item["segments"]) == 2


async def test_get_local_library_for_sync_excludes_non_done_jobs():
    await db.upsert_scan_job(
        plex_guid="pending-guid",
        title="Pending",
        file_path="/p.mkv",
        rating_key="50",
        library_id="1",
        library_title="L",
    )
    # Status left as 'pending'
    result = await db.get_local_library_for_sync()
    guids = [r["plex_guid"] for r in result]
    assert "pending-guid" not in guids


async def test_get_local_library_for_sync_job_with_no_segments():
    await db.upsert_scan_job(
        plex_guid="no-segs-guid",
        title="Empty",
        file_path="/e.mkv",
        rating_key="51",
        library_id="1",
        library_title="L",
    )
    await db.update_scan_job_status("no-segs-guid", "done")
    result = await db.get_local_library_for_sync()
    item = next((r for r in result if r["plex_guid"] == "no-segs-guid"), None)
    assert item is not None
    assert item["segments_count"] == 0


# ── Background Jobs ────────────────────────────────────────────────────────────

async def test_create_and_get_bg_job():
    job_id = await db.create_bg_job("upload")
    job = await db.get_bg_job(job_id)
    assert job is not None
    assert job["job_type"] == "upload"
    assert job["status"] == "running"


async def test_get_bg_job_returns_none_for_missing():
    result = await db.get_bg_job(99999)
    assert result is None


async def test_update_bg_job_status_and_progress():
    job_id = await db.create_bg_job("upload")
    await db.update_bg_job(job_id, status="completed", progress=100)
    job = await db.get_bg_job(job_id)
    assert job["status"] == "completed"
    assert job["progress_percent"] == 100
    assert job["completed_at"] is not None


async def test_update_bg_job_error():
    job_id = await db.create_bg_job("upload")
    await db.update_bg_job(job_id, status="failed", error="disk full")
    job = await db.get_bg_job(job_id)
    assert job["error_message"] == "disk full"


# ── Segment Library Entries ────────────────────────────────────────────────────

async def test_upsert_and_get_segment_library_entry():
    entry_id = await db.upsert_segment_library_entry(
        file_hash="abc123",
        file_name="movie.mkv",
        file_size=1000,
        duration_ms=90000,
        segments_json='[{"start_ms":0,"end_ms":1000}]',
        source_instance="instance-1",
        confidence_level="local",
    )
    assert entry_id > 0
    rows = await db.get_segment_library_entries_by_hash("abc123")
    assert len(rows) == 1
    assert rows[0]["source_instance"] == "instance-1"


async def test_upsert_segment_library_entry_updates_on_conflict():
    await db.upsert_segment_library_entry(
        file_hash="hash1", file_name="f.mkv", file_size=1, duration_ms=1,
        segments_json="[]", source_instance="inst", confidence_level="local",
    )
    await db.upsert_segment_library_entry(
        file_hash="hash1", file_name="f.mkv", file_size=1, duration_ms=1,
        segments_json='[{"x":1}]', source_instance="inst", confidence_level="verified",
    )
    rows = await db.get_segment_library_entries_by_hash("hash1")
    assert len(rows) == 1
    assert rows[0]["confidence_level"] == "verified"


async def test_get_segment_library_entries_by_hash_returns_empty():
    rows = await db.get_segment_library_entries_by_hash("no-such-hash")
    assert rows == []


async def test_get_segment_library_entries_by_hashes():
    await db.upsert_segment_library_entry(
        file_hash="h1", file_name="a.mkv", file_size=1, duration_ms=1,
        segments_json="[]", source_instance="x", confidence_level="local",
    )
    await db.upsert_segment_library_entry(
        file_hash="h2", file_name="b.mkv", file_size=1, duration_ms=1,
        segments_json="[]", source_instance="y", confidence_level="local",
    )
    rows = await db.get_segment_library_entries_by_hashes(["h1", "h2"])
    hashes = {r["file_hash"] for r in rows}
    assert hashes == {"h1", "h2"}


async def test_get_segment_library_entries_by_hashes_empty_returns_empty():
    rows = await db.get_segment_library_entries_by_hashes([])
    assert rows == []
