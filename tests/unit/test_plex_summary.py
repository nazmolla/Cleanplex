"""The Cleanplex block in a title's Plex summary (#136)."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cleanplex import database as db
from cleanplex import plex_summary
from cleanplex.plex_client import PlexClient

pytestmark = pytest.mark.usefixtures("setup_db")


@pytest.fixture(autouse=True)
def utc_clock(monkeypatch):
    """Timestamps are shown in local time; pin it so the expected text does not depend on the host."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


async def _title(status="done", duration_ms=5_280_000, rating_key="7"):
    await db.upsert_scan_job(plex_guid="g", title="T", file_path="/m.mkv", rating_key=rating_key,
                             library_id="l", library_title="L")
    async with db.get_connection() as conn:
        await conn.execute("UPDATE scan_jobs SET status=?, duration_ms=?, finished_at='2026-10-29 10:00:00' "
                           "WHERE plex_guid='g'", (status, duration_ms))
        await conn.commit()


async def _lines():
    return plex_summary.describe(await db.get_summary_stats("g"))


async def test_a_scanned_title_is_described_by_what_was_found():
    await _title()
    await db.insert_segment("g", "T", 100_000, 160_000, labels="FEMALE_BREAST_EXPOSED", severity="medium")
    await db.insert_segment("g", "T", 300_000, 330_000, labels="FEMALE_BREAST_EXPOSED,MALE_GENITALIA_EXPOSED",
                            severity="high", source="siglip")

    assert await _lines() == [
        "Cleanplex scan: 2 segments found (scanned 2026-10-29 10:00)",
        "Flagged time: 1m 30s, 2% of the runtime (1h 28m)",
        "Found: exposed female breast ×2, exposed male genitalia ×1",
        "Severity: 1 high, 1 medium",
        "From: NudeNet 1, SigLIP 1",
    ]


async def test_overlapping_segments_count_their_shared_time_once():
    await _title()
    await db.insert_segment("g", "T", 100_000, 200_000, labels="BUTTOCKS_EXPOSED")
    await db.insert_segment("g", "T", 150_000, 250_000, labels="BUTTOCKS_EXPOSED")
    assert "Flagged time: 2m 30s" in (await _lines())[1]


async def test_a_title_with_nothing_found_says_so():
    await _title()
    assert await _lines() == ["Cleanplex scan: nothing found (scanned 2026-10-29 10:00)"]


async def test_pending_and_failed_titles_say_that_instead_of_a_count():
    await _title(status="pending")
    assert (await _lines())[0] == "Cleanplex scan: pending"
    await _title(status="failed")
    assert (await _lines())[0] == "Cleanplex scan: failed"


async def test_false_positives_are_left_out_of_the_counts_and_reported_as_reviewed():
    await _title()
    keep = await db.insert_segment("g", "T", 10_000, 20_000, labels="BUTTOCKS_EXPOSED")
    wrong = await db.insert_segment("g", "T", 50_000, 60_000, labels="BUTTOCKS_EXPOSED")
    await db.mark_segment_false_positive(wrong)

    lines = await _lines()

    assert lines[0].startswith("Cleanplex scan: 1 segment found")
    assert lines[-1] == "Reviewed: 1 marked as false positive"
    assert keep


async def test_corrections_are_reported_and_the_new_label_is_the_one_shown():
    await _title()
    sid = await db.insert_segment("g", "T", 10_000, 20_000, labels="BUTTOCKS_EXPOSED", severity="medium")
    await db.relabel_segment(sid, ["FEMALE_BREAST_EXPOSED"])

    lines = await _lines()

    assert "Found: exposed female breast ×1" in lines
    assert lines[-1] == "Reviewed: 1 label corrected"


async def test_deleting_a_segment_changes_the_description():
    await _title()
    a = await db.insert_segment("g", "T", 10_000, 20_000, labels="BUTTOCKS_EXPOSED")
    await db.insert_segment("g", "T", 50_000, 60_000, labels="BUTTOCKS_EXPOSED")
    await db.delete_segment(a)
    assert (await _lines())[0].startswith("Cleanplex scan: 1 segment found")


async def test_unknown_title_has_no_stats():
    assert await db.get_summary_stats("nope") is None


@pytest.mark.parametrize("ms, text", [(0, "0s"), (59_000, "59s"), (252_000, "4m 12s"), (5_280_000, "1h 28m")])
def test_duration_text(ms, text):
    assert plex_summary.duration_text(ms) == text


# ── writing it ──────────────────────────────────────────────────────────────────

async def test_refresh_now_writes_the_described_lines_for_the_titles_rating_key():
    await _title()
    await db.insert_segment("g", "T", 10_000, 20_000, labels="BUTTOCKS_EXPOSED")
    client = MagicMock()
    client.update_cleanplex_summary = AsyncMock(return_value=True)

    with patch.object(plex_summary.plex_mod, "get_client", return_value=client):
        await plex_summary.refresh_now("g")

    kwargs = client.update_cleanplex_summary.call_args.kwargs
    assert kwargs["rating_key"] == "7"
    assert kwargs["details"][0].startswith("Cleanplex scan: 1 segment found")


async def test_refresh_now_skips_a_title_without_a_rating_key_or_scan_job():
    client = MagicMock()
    client.update_cleanplex_summary = AsyncMock()
    with patch.object(plex_summary.plex_mod, "get_client", return_value=client):
        await plex_summary.refresh_now("unknown")
        await _title(rating_key="")
        await plex_summary.refresh_now("g")
    client.update_cleanplex_summary.assert_not_called()


async def test_a_plex_failure_does_not_raise():
    await _title()
    with patch.object(plex_summary.plex_mod, "get_client", side_effect=RuntimeError("plex down")):
        await plex_summary.refresh_now("g")


async def test_a_burst_of_changes_becomes_one_write(monkeypatch):
    await _title()
    monkeypatch.setattr(plex_summary, "DEBOUNCE_S", 0.05)
    calls = []

    async def fake(guid):
        calls.append(guid)

    monkeypatch.setattr(plex_summary, "refresh_now", fake)
    for _ in range(5):
        plex_summary.refresh_soon("g")
    await asyncio.sleep(0.3)
    assert calls == ["g"]


# ── the Plex client side ────────────────────────────────────────────────────────

def test_block_with_details_replaces_the_status_lines():
    block = PlexClient("http://plex", "t")._build_cleanplex_block("Scanned", 5, details=["A", "B"])
    assert block == "[[CLEANPLEX]]\nA\nB\n[[/CLEANPLEX]]"


async def test_an_unchanged_block_is_not_written_again():
    c = PlexClient("http://plex", "t")
    block = c._build_cleanplex_block("x", 0, details=["Cleanplex scan: nothing found"])
    item = MagicMock(summary=f"Plot.\n\n{block}")
    item.editSummary = MagicMock()

    async def fake_to_thread(func, *args, **kwargs):
        return item if func is c._get_server else func(*args, **kwargs)

    server = MagicMock()
    server.fetchItem = MagicMock(return_value=item)
    c._server = server
    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=fake_to_thread):
        ok = await c.update_cleanplex_summary("7", "done", 0, details=["Cleanplex scan: nothing found"])

    assert ok is True
    item.editSummary.assert_not_called()


async def test_a_changed_block_replaces_the_old_one_and_keeps_the_plot():
    c = PlexClient("http://plex", "t")
    old = c._build_cleanplex_block("x", 0, details=["Cleanplex scan: nothing found"])
    item = MagicMock(summary=f"Plot.\n\n{old}")
    written = []
    item.editSummary = MagicMock(side_effect=written.append)

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    server = MagicMock()
    server.fetchItem = MagicMock(return_value=item)
    c._server = server
    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=fake_to_thread):
        await c.update_cleanplex_summary("7", "done", 3, details=["Cleanplex scan: 3 segments found"])

    assert written == ["Plot.\n\n[[CLEANPLEX]]\nCleanplex scan: 3 segments found\n[[/CLEANPLEX]]"]


def test_local_time_converts_from_utc(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    assert plex_summary.local_time("2026-10-29 14:00:00") == "2026-10-29 10:00"


def test_local_time_keeps_an_unparseable_stamp_readable():
    assert plex_summary.local_time("yesterday-ish") == "yesterday-ish"
