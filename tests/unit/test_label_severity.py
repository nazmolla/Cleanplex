"""Severity follows the detected label, and what each viewer level skips (#126)."""

from __future__ import annotations

import pytest

from cleanplex import database as db
from cleanplex import filter_engine
from cleanplex.importers._common import DETECTOR_LABELS, LABEL_SEVERITY, severity_for_labels


@pytest.mark.parametrize("labels, expected", [
    ("MALE_GENITALIA_EXPOSED", "high"),
    ("FEMALE_GENITALIA_EXPOSED", "high"),
    ("ANUS_EXPOSED", "high"),
    ("FEMALE_BREAST_EXPOSED", "medium"),
    ("BUTTOCKS_EXPOSED", "medium"),
    ("FEMALE_BREAST_COVERED", "low"),
    ("BUTTOCKS_COVERED,FEMALE_GENITALIA_COVERED", "low"),
    ("FACE_FEMALE", "low"),
    ("FEMALE_BREAST_COVERED,BUTTOCKS_EXPOSED", "medium"),
    ("FEMALE_BREAST_EXPOSED,MALE_GENITALIA_EXPOSED", "high"),
])
def test_severity_is_the_highest_among_the_labels(labels, expected):
    assert severity_for_labels(labels) == expected


def test_no_labels_falls_back_to_the_default():
    assert severity_for_labels("") == "high"
    assert severity_for_labels("", default="medium") == "medium"


def test_every_mapped_label_is_in_the_detector_vocabulary():
    assert set(LABEL_SEVERITY) <= set(DETECTOR_LABELS)


def _fires(labels: str, level: int) -> bool:
    seg = {"category": "nudity", "severity": severity_for_labels(labels)}
    return filter_engine._is_filtered(seg, {"nudity": {"level": level, "action": ""}})


@pytest.mark.parametrize("labels, skipped_at", [
    ("FEMALE_BREAST_COVERED", {3}),
    ("BUTTOCKS_EXPOSED", {2, 3}),
    ("FEMALE_BREAST_EXPOSED", {2, 3}),
    ("MALE_GENITALIA_EXPOSED", {1, 2, 3}),
    ("ANUS_EXPOSED", {1, 2, 3}),
])
def test_each_level_skips_exactly_the_labels_it_promises(labels, skipped_at):
    assert {lvl for lvl in range(4) if _fires(labels, lvl)} == skipped_at


# ── migration ──────────────────────────────────────────────────────────────────

pytestmark_db = pytest.mark.usefixtures("setup_db")


@pytest.mark.usefixtures("setup_db")
async def test_init_db_rederives_detector_severity_once_and_spares_imports():
    await db.upsert_scan_job(plex_guid="g", title="T", file_path="/m.mkv", rating_key="1",
                             library_id="l", library_title="L")
    scanner_seg = await db.insert_segment("g", "T", 0, 1000, labels="MALE_GENITALIA_EXPOSED", severity="medium")
    covered = await db.insert_segment("g", "T", 5000, 6000, labels="FEMALE_BREAST_COVERED", severity="high")
    imported = await db.insert_segment("g", "T", 9000, 9500, labels="FEMALE_BREAST_COVERED",
                                       severity="high", source="skp")
    async with db.get_connection() as conn:
        await conn.execute("DELETE FROM settings WHERE key='label_severity_migrated'")
        await conn.commit()

    await db.init_db()

    assert (await db.get_segment_by_id(scanner_seg))["severity"] == "high"
    assert (await db.get_segment_by_id(covered))["severity"] == "low"
    assert (await db.get_segment_by_id(imported))["severity"] == "high"

    # Marked as done: a user's later edit is not overwritten by the next startup.
    async with db.get_connection() as conn:
        await conn.execute("UPDATE segments SET severity='medium' WHERE id=?", (scanner_seg,))
        await conn.commit()
    await db.init_db()
    assert (await db.get_segment_by_id(scanner_seg))["severity"] == "medium"
