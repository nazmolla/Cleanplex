"""Library sync: retrying failed titles (#190) and the lifecycle of removed titles (#191)."""

from __future__ import annotations

import os
import time
from unittest.mock import patch

import pytest

from cleanplex import database as db
from cleanplex import segment_files
from cleanplex.plex_client import MediaItem
from tests.conftest import make_mock_plex_client

pytestmark = pytest.mark.usefixtures("setup_db")


def _item(guid, path, rating_key="1", library="lib", parts=None):
    return MediaItem(
        rating_key=rating_key, plex_guid=guid, title=guid, year=2020, thumb="", file_path=path,
        library_id=library, library_title="Lib", media_type="movie", part_files=parts or [path],
    )


async def _job(guid, path, rating_key="1", library="lib", part_files=""):
    await db.upsert_scan_job(plex_guid=guid, title=guid, file_path=str(path), rating_key=rating_key,
                             library_id=library, library_title="Lib", part_files=part_files)


async def _fail(guid, finished="now"):
    await db.update_scan_job_status(guid, "failed", error_msg="Could not determine video duration")
    if finished != "now":
        async with db.get_connection() as conn:
            await conn.execute("UPDATE scan_jobs SET finished_at=datetime('now', ?) WHERE plex_guid=?", (finished, guid))
            await conn.commit()


async def _sync(http_client, items):
    with patch("cleanplex.web.routes.segments.plex_mod.get_client", return_value=make_mock_plex_client(library_items=items)):
        resp = await http_client.post("/api/libraries/lib/sync")
    assert resp.status_code == 200
    return resp.json()


async def _status(guid):
    return (await db.get_scan_job_by_guid(guid))["status"]


# ── Failed titles (#190) ──────────────────────────────────────────────────────

async def test_titles_payload_carries_the_failure_reason_only_for_failed_titles(http_client):
    await _job("bad", "/bad.mkv")
    await _fail("bad")
    await _job("good", "/good.mkv", rating_key="2")
    await db.update_scan_job_status("good", "done")

    with patch("cleanplex.web.routes.segments.plex_mod.get_client", side_effect=RuntimeError):
        titles = {t["plex_guid"]: t for t in (await http_client.get("/api/libraries/lib/titles")).json()["titles"]}

    assert titles["bad"]["error_msg"] == "Could not determine video duration"
    assert titles["good"]["error_msg"] == ""


async def test_sync_requeues_a_failed_title_whose_file_moved(http_client, tmp_path):
    await _job("moved", tmp_path / "old.mkv")
    await _fail("moved")
    new = tmp_path / "new.mkv"
    new.write_bytes(b"x")
    os.utime(new, (time.time() - 86400, time.time() - 86400))

    data = await _sync(http_client, [_item("moved", str(new))])

    assert data["retried"] == 1
    assert await _status("moved") == "pending"
    assert (await db.get_scan_job_by_guid("moved"))["file_path"] == str(new)


async def test_sync_requeues_a_failed_title_whose_file_is_newer_than_the_failure(http_client, tmp_path):
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"x")
    await _job("grew", media)
    await _fail("grew", finished="-1 hours")

    data = await _sync(http_client, [_item("grew", str(media))])

    assert data["retried"] == 1
    assert await _status("grew") == "pending"


async def test_sync_requeues_a_title_that_failed_over_a_day_ago(http_client, tmp_path):
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"x")
    os.utime(media, (time.time() - 5 * 86400, time.time() - 5 * 86400))
    await _job("stale", media)
    await _fail("stale", finished="-2 days")

    data = await _sync(http_client, [_item("stale", str(media))])

    assert data["retried"] == 1
    assert await _status("stale") == "pending"


async def test_sync_leaves_a_recent_unchanged_failure_alone(http_client, tmp_path):
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"x")
    os.utime(media, (time.time() - 5 * 86400, time.time() - 5 * 86400))
    await _job("recent", media)
    await _fail("recent", finished="-1 hours")

    data = await _sync(http_client, [_item("recent", str(media))])

    assert data["retried"] == 0
    assert await _status("recent") == "failed"


async def test_sync_does_not_requeue_an_ignored_failed_title(http_client, tmp_path):
    await _job("skipme", tmp_path / "old.mkv")
    await _fail("skipme")
    await db.set_ignored("skipme", True)

    data = await _sync(http_client, [_item("skipme", str(tmp_path / "new.mkv"))])

    assert data["retried"] == 0
    assert await _status("skipme") == "failed"


# ── Removed titles (#191) ─────────────────────────────────────────────────────

async def _remove_via_sync(http_client, guid, keep=("keeper",)):
    """Sync a library whose Plex listing no longer holds *guid*."""
    return await _sync(http_client, [_item(k, f"/{k}.mkv", rating_key="900") for k in keep])


async def test_removed_title_segments_leave_every_live_list(http_client):
    await _job("keeper", "/keeper.mkv", rating_key="900")
    await _job("gone", "/gone.mkv", rating_key="901")
    await db.insert_segment("keeper", "Keeper", 0, 1000)
    await db.insert_segment("gone", "Gone", 0, 1000)

    data = await _remove_via_sync(http_client, "gone")

    assert data["removed"] == 1
    assert await db.get_scan_job_by_guid("gone") is None
    assert [s["plex_guid"] for s in await db.get_all_segments()] == ["keeper"]
    assert await db.get_segments_for_guid("gone") == []
    assert len(await db.get_removed_segments("gone")) == 1


async def test_a_title_that_comes_back_gets_its_marks_back(http_client):
    await _job("keeper", "/keeper.mkv", rating_key="900")
    await _job("gone", "/gone.mkv", rating_key="901")
    seg = await db.insert_segment("gone", "Gone", 5000, 9000, labels="BUTTOCKS_EXPOSED")
    await db.mark_segment_false_positive(seg)
    await _remove_via_sync(http_client, "gone")

    await _sync(http_client, [_item("keeper", "/keeper.mkv", rating_key="900"), _item("gone", "/gone.mkv", rating_key="901")])

    restored = await db.get_segment_by_id(seg)
    assert (restored["start_ms"], restored["end_ms"], restored["false_positive"]) == (5000, 9000, 1)
    assert await db.get_removed_segments("gone") == []
    assert await db.get_removed_title("gone") is None


async def test_segments_left_without_a_job_by_older_syncs_are_archived():
    await _job("live", "/live.mkv")
    await db.insert_segment("live", "Live", 0, 1000)
    old = await db.insert_segment("orphan", "Orphan", 0, 1000)
    async with db.get_connection() as conn:
        await conn.execute("UPDATE segments SET created_at=datetime('now','-3 days') WHERE id=?", (old,))
        await conn.commit()
    fresh = await db.insert_segment("orphan2", "Fresh", 0, 1000)

    assert await db.archive_orphan_segments() == 1

    assert [s["plex_guid"] for s in await db.get_all_segments()] == ["live", "orphan2"]
    assert len(await db.get_removed_segments("orphan")) == 1
    assert (await db.get_removed_title("orphan"))["title"] == "Orphan"
    assert (await db.get_segment_by_id(fresh)) is not None


async def _removed_title_with_data(http_client, tmp_path, rating_key="901", age="-40 days"):
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"jpg")
    await _job("keeper", "/keeper.mkv", rating_key="900")
    await _job("gone", "/gone.mkv", rating_key=rating_key)
    seg = await db.insert_segment("gone", "Gone", 0, 1000, thumbnail_path=str(thumb))
    await db.record_skip_event(plex_guid="gone", title="Gone", username="u", client_identifier="c", client_title="C",
                               category="nudity", action="skip", segment_id=seg, position_ms=0, target_ms=1000,
                               success=True, latency_ms=1)
    await db.upsert_plex_markers("gone", rating_key, [{"marker_type": "intro", "start_ms": 0, "end_ms": 5, "plex_marker_id": "1", "final": False}])
    await db.replace_cast({rating_key: ["Actor"]})
    await db.put_keyframes("/gone.mkv", 1, 1, [0, 1000])
    await db.replace_title_parts([(77, rating_key)])
    await _remove_via_sync(http_client, "gone")
    async with db.get_connection() as conn:
        await conn.execute("UPDATE removed_titles SET removed_at=datetime('now', ?)", (age,))
        await conn.commit()
    return seg, thumb


async def _count(table, where="1=1", params=()):
    async with db.get_connection() as conn:
        return (await (await conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params)).fetchone())[0]


async def test_purge_after_retention_deletes_saved_data_but_keeps_the_marks(http_client, tmp_path):
    seg, thumb = await _removed_title_with_data(http_client, tmp_path)

    refs = await db.purge_removed_titles(30)

    assert [r[0] for r in refs] == [seg]
    assert await _count("skip_events", "plex_guid='gone'") == 0
    assert await _count("plex_markers", "plex_guid='gone'") == 0
    assert await _count("title_cast", "rating_key='901'") == 0
    assert await _count("title_parts", "rating_key='901'") == 0
    assert await _count("keyframe_index", "file_path='/gone.mkv'") == 0
    marks = await db.get_removed_segments("gone")
    assert [(m["id"], m["start_ms"], m["thumbnail_path"]) for m in marks] == [(seg, 0, "")]
    # A second run has nothing left to purge.
    assert await db.purge_removed_titles(30) == []


async def test_purge_waits_out_the_retention_period(http_client, tmp_path):
    seg, thumb = await _removed_title_with_data(http_client, tmp_path, age="-2 days")

    assert await db.purge_removed_titles(30) == []
    assert await _count("skip_events", "plex_guid='gone'") == 1
    assert (await db.get_removed_segments("gone"))[0]["thumbnail_path"] == str(thumb)


async def test_purge_keeps_cast_and_keyframes_still_used_by_a_live_title(http_client, tmp_path):
    # A re-matched title: the old guid is removed but the live one keeps the rating key and file.
    await _job("live", "/gone.mkv", rating_key="901", library="other")
    await _removed_title_with_data(http_client, tmp_path, rating_key="901")

    await db.purge_removed_titles(30)

    assert await _count("title_cast", "rating_key='901'") == 1
    assert await _count("keyframe_index", "file_path='/gone.mkv'") == 1


async def test_sweep_deletes_the_files_of_purged_marks(http_client, tmp_path):
    seg, thumb = await _removed_title_with_data(http_client, tmp_path)

    report = await segment_files.sweep(tmp_path / "thumbs", posters_dir=tmp_path / "posters", previews_dir=tmp_path / "previews")

    assert not thumb.exists()
    assert report["purged_segments"] == 1
    assert len(await db.get_removed_segments("gone")) == 1
