"""False-positive marking routes (issue #101)."""

from __future__ import annotations

import pytest

from cleanplex import database as db
from cleanplex import events
from cleanplex.web.routes import segments as seg_routes

pytestmark = pytest.mark.usefixtures("setup_db")


@pytest.fixture
def feedback_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(seg_routes, "FEEDBACK_DIR", tmp_path / "feedback")
    return tmp_path / "feedback"


async def _segment(tmp_path):
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"\xff\xd8 frame")
    await db.upsert_scan_job(plex_guid="g", title="T", file_path="/x", rating_key="1",
                             library_id="lib", library_title="Lib")
    return await db.insert_segment("g", "T", 0, 1000, thumbnail_path=str(thumb), confidence=0.8,
                                   source="siglip", detector="siglip-base-224")


async def test_marking_keeps_a_copy_of_the_frame_and_lists_the_segment_as_marked(http_client, tmp_path, feedback_dir):
    seg = await _segment(tmp_path)

    with events.subscribe() as q:
        resp = await http_client.post(f"/api/segments/{seg}/false-positive", json={"false_positive": True})
        published = [q.get_nowait() for _ in range(q.qsize())]

    assert resp.status_code == 200
    assert ("segments_changed", {"guid": "g"}) in published
    copies = list(feedback_dir.iterdir())
    assert len(copies) == 1 and copies[0].read_bytes() == b"\xff\xd8 frame"
    listed = (await http_client.get("/api/titles/g/segments")).json()["segments"]
    assert [(s["id"], s["false_positive"]) for s in listed] == [(seg, True)]


async def test_undo_removes_the_saved_frame(http_client, tmp_path, feedback_dir):
    seg = await _segment(tmp_path)
    await http_client.post(f"/api/segments/{seg}/false-positive", json={"false_positive": True})

    resp = await http_client.post(f"/api/segments/{seg}/false-positive", json={"false_positive": False})

    assert resp.status_code == 200
    assert list(feedback_dir.iterdir()) == []
    listed = (await http_client.get("/api/titles/g/segments")).json()["segments"]
    assert listed[0]["false_positive"] is False


async def test_marking_an_unknown_segment_is_404(http_client, feedback_dir):
    assert (await http_client.post("/api/segments/999/false-positive", json={"false_positive": True})).status_code == 404


async def test_a_missing_body_field_is_422(http_client, tmp_path, feedback_dir):
    seg = await _segment(tmp_path)

    assert (await http_client.post(f"/api/segments/{seg}/false-positive", json={})).status_code == 422


async def test_accuracy_report_counts_marks_per_detector_and_label(http_client, tmp_path, feedback_dir):
    seg = await _segment(tmp_path)
    await db.insert_segment("g", "T", 5000, 6000, confidence=0.6, source="siglip", detector="siglip-base-224")
    await http_client.post(f"/api/segments/{seg}/false-positive", json={"false_positive": True})

    body = (await http_client.get("/api/analytics/detection-accuracy")).json()

    assert body["marked"] == 1
    assert body["rows"][0]["total"] == 2
    assert body["rows"][0]["false_positive_rate"] == 0.5
