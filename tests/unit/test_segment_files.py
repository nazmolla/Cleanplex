"""Deleting a segment's files and sweeping orphans (#153)."""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, patch

import pytest

from cleanplex import database as db
from cleanplex import hover_previews, segment_files

pytestmark = pytest.mark.usefixtures("setup_db")

OLD = time.time() - 3 * 3600  # older than the grace period


@pytest.fixture(autouse=True)
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(hover_previews, "HOVER_DIR", tmp_path / "hover")
    monkeypatch.setattr(segment_files, "FEEDBACK_DIR", tmp_path / "feedback")
    for d in ("thumbs", "hover", "feedback"):
        (tmp_path / d).mkdir()
    return tmp_path


def _file(path, age=OLD, data=b"x" * 10):
    path.write_bytes(data)
    os.utime(path, (age, age))
    return path


async def _job(guid="g"):
    await db.upsert_scan_job(plex_guid=guid, title="T", file_path="/m.mkv", rating_key="1",
                             library_id="l", library_title="L")


async def _segment(dirs, guid="g", start=0, hover=True, thumb=True):
    sid = await db.insert_segment(guid, "T", start, start + 1000,
                                  thumbnail_path=str(dirs / "thumbs" / f"{guid}_{start}.jpg") if thumb else None)
    if thumb:
        _file(dirs / "thumbs" / f"{guid}_{start}.jpg", age=time.time())
    if hover:
        _file(hover_previews.hover_path(sid), age=time.time())
    return sid


# ── deleting a segment removes its files ────────────────────────────────────────

async def test_deleting_a_segment_removes_its_thumbnail_and_hover_preview(dirs):
    await _job()
    keep = await _segment(dirs, start=0)
    gone = await _segment(dirs, start=5000)

    assert await segment_files.delete_segment(gone) is True

    assert not (dirs / "thumbs" / "g_5000.jpg").exists() and not hover_previews.hover_path(gone).exists()
    assert (dirs / "thumbs" / "g_0.jpg").exists() and hover_previews.hover_path(keep).exists()
    assert await db.get_segment_by_id(gone) is None


async def test_deleting_an_unknown_segment_is_false_and_touches_nothing(dirs):
    await _job()
    await _segment(dirs)
    assert await segment_files.delete_segment(999) is False
    assert (dirs / "thumbs" / "g_0.jpg").exists()


async def test_a_segment_whose_files_are_already_gone_deletes_cleanly(dirs):
    await _job()
    sid = await _segment(dirs, hover=False, thumb=False)
    assert await segment_files.delete_segment(sid) is True


async def test_deleting_a_titles_segments_removes_only_that_titles_files(dirs):
    await _job("g")
    await _job("h")
    a = await _segment(dirs, "g", 0)
    b = await _segment(dirs, "g", 5000)
    other = await _segment(dirs, "h", 0)

    assert await segment_files.delete_segments_for_guid("g") == 2

    for sid, name in ((a, "g_0"), (b, "g_5000")):
        assert not (dirs / "thumbs" / f"{name}.jpg").exists() and not hover_previews.hover_path(sid).exists()
    assert (dirs / "thumbs" / "h_0.jpg").exists() and hover_previews.hover_path(other).exists()


# ── the sweep ───────────────────────────────────────────────────────────────────

async def test_sweep_removes_unreferenced_old_thumbnails_and_keeps_the_rest(dirs):
    await _job()
    await _segment(dirs, start=0)  # referenced (and young)
    orphan = _file(dirs / "thumbs" / "old_orphan.jpg")
    young = _file(dirs / "thumbs" / "young_orphan.jpg", age=time.time())

    report = await segment_files.sweep(dirs / "thumbs")

    assert not orphan.exists()
    assert young.exists()  # may belong to a segment a scan is about to insert
    assert (dirs / "thumbs" / "g_0.jpg").exists()
    assert report["thumbnails"] == 1 and report["bytes"] == 10


async def test_a_referenced_thumbnail_is_kept_however_old(dirs):
    await _job()
    sid = await db.insert_segment("g", "T", 0, 1000, thumbnail_path=str(dirs / "thumbs" / "g_0.jpg"))
    _file(dirs / "thumbs" / "g_0.jpg")
    await segment_files.sweep(dirs / "thumbs")
    assert (dirs / "thumbs" / "g_0.jpg").exists() and sid


async def test_sweep_removes_hover_previews_of_segments_that_are_gone_but_keeps_false_positives(dirs):
    await _job()
    live = await _segment(dirs, start=0)
    wrong = await _segment(dirs, start=5000)
    await db.mark_segment_false_positive(wrong)
    ghost = _file(hover_previews.hover_path(777))
    leftover = _file(hover_previews.HOVER_DIR / "5.mp4.part")

    report = await segment_files.sweep(dirs / "thumbs")

    assert not ghost.exists() and not leftover.exists()
    assert hover_previews.hover_path(live).exists() and hover_previews.hover_path(wrong).exists()
    assert report["hover"] == 2


async def test_sweep_keeps_feedback_frames_a_row_owns_and_removes_the_rest(dirs):
    await _job()
    sid = await _segment(dirs)
    owned = _file(dirs / "feedback" / "owned.jpg")
    await db.mark_segment_false_positive(sid, str(owned))
    orphan = _file(dirs / "feedback" / "orphan.jpg")
    young = _file(dirs / "feedback" / "young.jpg", age=time.time())

    report = await segment_files.sweep(dirs / "thumbs")

    assert owned.exists() and young.exists() and not orphan.exists()
    assert report["feedback_frames"] == 1


async def test_a_frame_outlives_the_segment_it_came_from(dirs):
    await _job()
    sid = await _segment(dirs)
    frame = _file(dirs / "feedback" / "kept.jpg")
    await db.mark_segment_false_positive(sid, str(frame))
    await segment_files.delete_segment(sid)
    await segment_files.sweep(dirs / "thumbs")
    assert frame.exists()


async def test_segments_of_a_removed_title_are_deleted_after_the_cutoff_with_their_files(dirs):
    sid = await _segment(dirs, guid="gone", start=0)
    old = await _segment(dirs, guid="gone", start=5000)
    async with db.get_connection() as conn:
        await conn.execute("UPDATE segments SET created_at = datetime('now', '-40 days') WHERE id = ?", (old,))
        await conn.commit()

    report = await segment_files.sweep(dirs / "thumbs")

    assert report["orphan_segments"] == 1
    assert await db.get_segment_by_id(old) is None
    assert not (dirs / "thumbs" / "gone_5000.jpg").exists() and not hover_previews.hover_path(old).exists()
    assert await db.get_segment_by_id(sid) is not None  # young: the title may come back


async def test_segments_of_a_title_that_still_exists_are_never_swept_as_orphans(dirs):
    await _job("g")
    sid = await _segment(dirs, guid="g")
    async with db.get_connection() as conn:
        await conn.execute("UPDATE segments SET created_at = datetime('now', '-400 days') WHERE id = ?", (sid,))
        await conn.commit()
    assert (await segment_files.sweep(dirs / "thumbs"))["orphan_segments"] == 0
    assert await db.get_segment_by_id(sid) is not None


async def test_a_second_sweep_finds_nothing_more(dirs):
    _file(dirs / "thumbs" / "x.jpg")
    await segment_files.sweep(dirs / "thumbs")
    report = await segment_files.sweep(dirs / "thumbs")
    assert not any(report.values())


async def test_a_missing_directory_is_not_an_error(tmp_path):
    report = await segment_files.sweep(tmp_path / "does-not-exist")
    assert report["thumbnails"] == 0


async def test_the_loop_survives_a_failed_sweep_and_keeps_going(monkeypatch):
    monkeypatch.setattr(segment_files, "STARTUP_DELAY_S", 0)
    sweeps = AsyncMock(side_effect=[RuntimeError("disk"), {"orphan_segments": 0, "thumbnails": 0, "hover": 0,
                                                           "feedback_frames": 0, "bytes": 0}])
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            raise KeyboardInterrupt  # ends the loop under test

    with patch.object(segment_files, "sweep", sweeps), patch.object(segment_files.asyncio, "sleep", fake_sleep):
        with pytest.raises(KeyboardInterrupt):
            await segment_files.segment_cleanup_loop()

    assert sweeps.await_count == 2 and sleeps[1:] == [segment_files.SWEEP_INTERVAL_S] * 2
