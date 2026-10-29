"""Rows the stream proxy decides on, and the client registry it consults (#132)."""

from __future__ import annotations

import pytest

from cleanplex import database as db

pytestmark = pytest.mark.usefixtures("setup_db")


async def _job(guid="g", rating_key="7"):
    await db.upsert_scan_job(plex_guid=guid, title="T", file_path="/m.mkv", rating_key=rating_key,
                             library_id="l", library_title="L")


async def test_returns_active_segments_ordered_by_start_with_their_classification():
    await _job()
    await db.insert_segment("g", "T", 50_000, 60_000, severity="low")
    await db.insert_segment("g", "T", 10_000, 20_000, action="mute", category="language")
    rows = await db.get_active_segments_for_rating_key("7")
    assert [(r["start_ms"], r["end_ms"], r["category"], r["severity"], r["action"]) for r in rows] == [
        (10_000, 20_000, "language", "high", "mute"),
        (50_000, 60_000, "nudity", "low", "skip"),
    ]


async def test_excludes_false_positives():
    await _job()
    fp = await db.insert_segment("g", "T", 10_000, 20_000)
    await db.mark_segment_false_positive(fp)
    await db.insert_segment("g", "T", 40_000, 45_000)
    assert [r["start_ms"] for r in await db.get_active_segments_for_rating_key("7")] == [40_000]


async def test_only_the_requested_title_is_returned():
    await _job("g", "7")
    await _job("h", "8")
    await db.insert_segment("g", "T", 1_000, 2_000)
    await db.insert_segment("h", "T", 5_000, 6_000)
    assert [r["start_ms"] for r in await db.get_active_segments_for_rating_key("8")] == [5_000]


async def test_unknown_rating_key_is_empty():
    assert await db.get_active_segments_for_rating_key("999") == []


async def test_client_control_is_none_until_recorded():
    assert await db.get_client_control("tv-1") is None


async def test_client_control_round_trips_and_updates():
    await db.record_client_control("tv-1", True)
    assert await db.get_client_control("tv-1") is True
    await db.record_client_control("tv-1", False)
    assert await db.get_client_control("tv-1") is False
