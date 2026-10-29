"""PUT /api/segments/{id}/labels (#125)."""

from __future__ import annotations

import pytest

from cleanplex import database as db
from cleanplex.web.routes import segments as segments_routes

pytestmark = pytest.mark.usefixtures("setup_db")


async def _seg(tmp_path, source="siglip"):
    await db.upsert_scan_job(plex_guid="g", title="T", file_path="/m.mkv", rating_key="1",
                             library_id="l", library_title="L")
    thumb = tmp_path / "t.jpg"
    thumb.write_bytes(b"jpg")
    sid = await db.insert_segment("g", "T", 0, 1000, labels="BUTTOCKS_EXPOSED", severity="medium",
                                  thumbnail_path=str(thumb), source=source)
    return sid


@pytest.fixture(autouse=True)
def feedback_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(segments_routes, "FEEDBACK_DIR", tmp_path / "feedback")
    monkeypatch.setattr(segments_routes, "_refresh_cleanplex_summary_for_guid", lambda guid: None)


async def test_relabel_returns_new_labels_and_severity_and_keeps_the_frame(http_client, tmp_path):
    sid = await _seg(tmp_path)

    resp = await http_client.put(f"/api/segments/{sid}/labels", json={"labels": ["MALE_GENITALIA_EXPOSED"]})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "segment_id": sid, "labels": "MALE_GENITALIA_EXPOSED", "severity": "high"}
    assert len(list((tmp_path / "feedback").glob("*.jpg"))) == 1


async def test_undo_removes_the_feedback_frame(http_client, tmp_path):
    sid = await _seg(tmp_path)
    await http_client.put(f"/api/segments/{sid}/labels", json={"labels": ["MALE_GENITALIA_EXPOSED"]})

    resp = await http_client.put(f"/api/segments/{sid}/labels", json={"labels": ["BUTTOCKS_EXPOSED"]})

    assert resp.json()["labels"] == "BUTTOCKS_EXPOSED"
    assert list((tmp_path / "feedback").glob("*.jpg")) == []


async def test_edit_of_an_existing_correction_leaves_one_frame(http_client, tmp_path):
    sid = await _seg(tmp_path)
    for label in ("MALE_GENITALIA_EXPOSED", "FEMALE_BREAST_EXPOSED"):
        await http_client.put(f"/api/segments/{sid}/labels", json={"labels": [label]})
    assert len(list((tmp_path / "feedback").glob("*.jpg"))) == 1


async def test_unknown_segment_is_404(http_client):
    resp = await http_client.put("/api/segments/999/labels", json={"labels": ["ANUS_EXPOSED"]})
    assert resp.status_code == 404


@pytest.mark.parametrize("body", [
    {"labels": []}, {"labels": ["NOT_A_LABEL"]}, {"labels": ["ANUS_EXPOSED", "nope"]}, {}, {"labels": "ANUS_EXPOSED"},
])
async def test_invalid_labels_are_422(http_client, tmp_path, body):
    sid = await _seg(tmp_path)
    resp = await http_client.put(f"/api/segments/{sid}/labels", json=body)
    assert resp.status_code == 422
    assert (await db.get_segment_by_id(sid))["labels"] == "BUTTOCKS_EXPOSED"


async def test_relabel_shows_in_analytics_under_the_new_label(http_client, tmp_path):
    sid = await _seg(tmp_path)
    await http_client.put(f"/api/segments/{sid}/labels", json={"labels": ["ANUS_EXPOSED"]})

    resp = await http_client.get("/api/analytics/segments?labels=ANUS_EXPOSED")

    assert [s["id"] for s in resp.json()["segments"]] == [sid]
