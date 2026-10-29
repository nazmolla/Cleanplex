"""get_skip_ranges_for_rating_key: the cut list the stream proxy applies (#132)."""

from __future__ import annotations

import pytest

from cleanplex import database as db

pytestmark = pytest.mark.usefixtures("setup_db")


async def _job(guid="g", rating_key="7"):
    await db.upsert_scan_job(plex_guid=guid, title="T", file_path="/m.mkv", rating_key=rating_key,
                             library_id="l", library_title="L")


async def test_returns_skip_segments_ordered_by_start():
    await _job()
    await db.insert_segment("g", "T", 50_000, 60_000)
    await db.insert_segment("g", "T", 10_000, 20_000)
    assert await db.get_skip_ranges_for_rating_key("7") == [(10_000, 20_000), (50_000, 60_000)]


async def test_excludes_false_positives_and_mutes():
    await _job()
    fp = await db.insert_segment("g", "T", 10_000, 20_000)
    await db.mark_segment_false_positive(fp)
    await db.insert_segment("g", "T", 30_000, 31_000, action="mute")
    await db.insert_segment("g", "T", 40_000, 45_000)
    assert await db.get_skip_ranges_for_rating_key("7") == [(40_000, 45_000)]


async def test_only_the_requested_title_is_returned():
    await _job("g", "7")
    await _job("h", "8")
    await db.insert_segment("g", "T", 1_000, 2_000)
    await db.insert_segment("h", "T", 5_000, 6_000)
    assert await db.get_skip_ranges_for_rating_key("8") == [(5_000, 6_000)]


async def test_unknown_rating_key_is_empty():
    assert await db.get_skip_ranges_for_rating_key("999") == []
