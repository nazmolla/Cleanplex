"""relabel_segment: correct a segment's labels and keep the correction as feedback (#125)."""

from __future__ import annotations

import pytest

from cleanplex import database as db

pytestmark = pytest.mark.usefixtures("setup_db")


async def _seg(labels="BUTTOCKS_EXPOSED", source="siglip", severity="medium", start=0, end=1000):
    await db.upsert_scan_job(plex_guid="g", title="T", file_path="/m.mkv", rating_key="1",
                             library_id="l", library_title="L")
    return await db.insert_segment("g", "T", start, end, confidence=0.9, labels=labels,
                                   severity=severity, source=source, detector="siglip-base-224")


async def _feedback(segment_id):
    async with db.get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM segment_feedback WHERE segment_id=? AND verdict='relabeled'", (segment_id,))
    return [dict(r) for r in rows]


async def test_relabel_updates_labels_and_rederives_severity():
    sid = await _seg()

    result = await db.relabel_segment(sid, ["MALE_GENITALIA_EXPOSED"], "/f.jpg")

    seg = await db.get_segment_by_id(sid)
    assert (seg["labels"], seg["severity"]) == ("MALE_GENITALIA_EXPOSED", "high")
    assert result == {"labels": "MALE_GENITALIA_EXPOSED", "severity": "high", "frames": []}


async def test_relabel_records_original_and_corrected_labels():
    sid = await _seg()

    await db.relabel_segment(sid, ["FEMALE_BREAST_EXPOSED", "BUTTOCKS_EXPOSED"], "/f.jpg")

    [row] = await _feedback(sid)
    assert row["labels"] == "BUTTOCKS_EXPOSED"
    assert row["corrected_labels"] == "FEMALE_BREAST_EXPOSED,BUTTOCKS_EXPOSED"
    assert (row["detector"], row["frame_path"], row["confidence"]) == ("siglip-base-224", "/f.jpg", 0.9)


async def test_second_edit_updates_the_same_feedback_row_and_keeps_the_original_labels():
    sid = await _seg()
    await db.relabel_segment(sid, ["FEMALE_BREAST_EXPOSED"], "/f1.jpg")

    result = await db.relabel_segment(sid, ["MALE_GENITALIA_EXPOSED"], "/f2.jpg")

    [row] = await _feedback(sid)
    assert row["labels"] == "BUTTOCKS_EXPOSED" and row["corrected_labels"] == "MALE_GENITALIA_EXPOSED"
    assert row["frame_path"] == "/f1.jpg"
    assert result["frames"] == ["/f2.jpg"]  # the new copy is not kept


async def test_relabeling_back_to_the_original_undoes_it():
    sid = await _seg()
    await db.relabel_segment(sid, ["FEMALE_BREAST_EXPOSED"], "/f1.jpg")

    result = await db.relabel_segment(sid, ["BUTTOCKS_EXPOSED"], "/f2.jpg")

    seg = await db.get_segment_by_id(sid)
    assert (seg["labels"], seg["severity"]) == ("BUTTOCKS_EXPOSED", "medium")
    assert await _feedback(sid) == []
    assert sorted(result["frames"]) == ["/f1.jpg", "/f2.jpg"]


async def test_duplicate_labels_are_collapsed():
    sid = await _seg()
    result = await db.relabel_segment(sid, ["ANUS_EXPOSED", "ANUS_EXPOSED"])
    assert result["labels"] == "ANUS_EXPOSED"


async def test_imported_segment_keeps_its_severity():
    sid = await _seg(labels="x", source="skp", severity="low")
    result = await db.relabel_segment(sid, ["MALE_GENITALIA_EXPOSED"])
    assert result["severity"] == "low"


async def test_unknown_segment_returns_none():
    assert await db.relabel_segment(999, ["ANUS_EXPOSED"]) is None


async def test_rescan_keeps_the_correction_for_an_overlapping_window():
    sid = await _seg(start=10_000, end=14_000)
    await db.relabel_segment(sid, ["MALE_GENITALIA_EXPOSED"], "/f.jpg")
    await db.delete_segment(sid)

    new = await db.insert_segment("g", "T", 10_500, 13_500, confidence=0.8, labels="BUTTOCKS_EXPOSED",
                                  severity="medium", source="siglip")

    seg = await db.get_segment_by_id(new)
    assert (seg["labels"], seg["severity"]) == ("MALE_GENITALIA_EXPOSED", "high")


async def test_rescan_correction_does_not_touch_a_non_overlapping_segment():
    sid = await _seg(start=10_000, end=14_000)
    await db.relabel_segment(sid, ["MALE_GENITALIA_EXPOSED"], "/f.jpg")

    other = await db.insert_segment("g", "T", 50_000, 51_000, labels="BUTTOCKS_EXPOSED", source="siglip")

    assert (await db.get_segment_by_id(other))["labels"] == "BUTTOCKS_EXPOSED"
