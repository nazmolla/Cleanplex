"""False-positive marks on segments (issue #101), against real SQLite."""

from __future__ import annotations

import pytest

from cleanplex import database as db

pytestmark = pytest.mark.usefixtures("setup_db")


async def _job(guid="g", library="lib"):
    await db.upsert_scan_job(plex_guid=guid, title="T", file_path="/x", rating_key="1",
                             library_id=library, library_title="Lib")
    await db.update_scan_job_status(guid, "done")


async def test_marked_segments_stop_acting_but_stay_reviewable():
    await _job()
    keep = await db.insert_segment("g", "T", 0, 1000)
    wrong = await db.insert_segment("g", "T", 5000, 6000)

    await db.mark_segment_false_positive(wrong)

    assert [s["id"] for s in await db.get_segments_for_guid("g")] == [keep]
    assert [s["id"] for s in await db.get_segments_for_guid("g", include_false_positives=True)] == [keep, wrong]
    assert await db.count_segments_for_guid("g") == 1
    assert await db.get_segment_counts_for_library("lib") == {"g": 1}
    assert await db.get_false_positive_counts_for_library("lib") == {"g": 1}


async def test_false_positive_counts_are_empty_without_marks():
    await _job()
    await db.insert_segment("g", "T", 0, 1000)

    assert await db.get_false_positive_counts_for_library("lib") == {}
    assert await db.get_false_positive_counts_for_library("no-such-lib") == {}


async def test_marked_segments_are_left_out_of_rating_key_lookup_and_sync():
    await _job()
    wrong = await db.insert_segment("g", "T", 5000, 6000)
    await db.mark_segment_false_positive(wrong)

    assert await db.get_segments_by_rating_key("1") == []
    titles = await db.get_local_library_for_sync()
    assert all(not t["segments"] for t in titles if t["plex_guid"] == "g")


async def test_marking_records_one_verdict_with_the_segment_details():
    await _job()
    seg = await db.insert_segment("g", "T", 5000, 6000, confidence=0.7, labels="BUTTOCKS_EXPOSED",
                                  source="siglip", detector="siglip-base-224")

    first = await db.mark_segment_false_positive(seg, "/frames/1.jpg")
    second = await db.mark_segment_false_positive(seg, "/frames/2.jpg")

    assert first == second
    rows = await db.get_detection_accuracy()
    assert rows == [{
        "source": "siglip", "detector": "siglip-base-224", "label": "BUTTOCKS_EXPOSED",
        "total": 1, "false_positives": 1, "verifier_rejected": 0, "avg_score_kept": None, "avg_score_false": pytest.approx(0.7),
    }]


async def test_a_rescan_does_not_bring_back_a_marked_window():
    await _job()
    await db.mark_segment_false_positive(await db.insert_segment("g", "T", 5000, 6000))

    await db.delete_segments_for_guid("g")
    overlapping = await db.insert_segment("g", "T", 5500, 7000, source="siglip")
    elsewhere = await db.insert_segment("g", "T", 9000, 9500)

    assert (await db.get_segment_by_id(overlapping))["false_positive"] == 1
    assert (await db.get_segment_by_id(elsewhere))["false_positive"] == 0


async def test_imported_segments_are_never_auto_marked():
    await _job()
    await db.mark_segment_false_positive(await db.insert_segment("g", "T", 5000, 6000))

    imported = await db.insert_segment("g", "T", 5000, 6000, source="skp")

    assert (await db.get_segment_by_id(imported))["false_positive"] == 0


async def test_undo_clears_the_flag_and_the_verdict_and_returns_its_frames():
    await _job()
    seg = await db.insert_segment("g", "T", 5000, 6000)
    await db.mark_segment_false_positive(seg, "/frames/1.jpg")

    frames = await db.unmark_segment_false_positive(seg)

    assert frames == ["/frames/1.jpg"]
    assert (await db.get_segment_by_id(seg))["false_positive"] == 0
    assert (await db.get_detection_accuracy())[0]["false_positives"] == 0


async def test_marking_or_undoing_an_unknown_segment_does_nothing():
    assert await db.mark_segment_false_positive(999) is None
    assert await db.unmark_segment_false_positive(999) == []
    assert await db.get_detection_accuracy() == []


async def test_false_positive_segments_list_newest_mark_first():
    await _job()
    first = await db.insert_segment("g", "T", 0, 1000)
    second = await db.insert_segment("g", "T", 5000, 6000)
    await db.insert_segment("g", "T", 9000, 9500)  # stays active
    await db.mark_segment_false_positive(first)
    await db.mark_segment_false_positive(second)

    rows = await db.get_false_positive_segments(10, 0)

    assert {r["id"] for r in rows} == {first, second}
    assert all(r["marked_at"] for r in rows)
    assert await db.count_false_positive_segments() == 2
    assert [r["id"] for r in await db.get_false_positive_segments(1, 1)] == [rows[1]["id"]]


async def test_false_positive_segments_are_empty_without_marks():
    await _job()
    await db.insert_segment("g", "T", 0, 1000)

    assert await db.get_false_positive_segments(10, 0) == []
    assert await db.count_false_positive_segments() == 0
