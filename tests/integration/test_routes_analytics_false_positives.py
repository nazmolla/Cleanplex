"""GET /api/analytics/false-positives (#117)."""

from __future__ import annotations

from cleanplex import database as db


async def _seed():
    await db.upsert_scan_job(plex_guid="g", title="Movie", file_path="/m.mkv", rating_key="7",
                             library_id="lib", library_title="Movies")
    wrong = await db.insert_segment("g", "Movie", 0, 1000, labels="FEMALE_BREAST_EXPOSED")
    await db.insert_segment("g", "Movie", 5000, 6000)
    await db.mark_segment_false_positive(wrong)
    return wrong


async def test_lists_marked_segments_with_total(http_client):
    wrong = await _seed()

    resp = await http_client.get("/api/analytics/false-positives")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    seg = body["segments"][0]
    assert (seg["id"], seg["false_positive"], seg["labels"]) == (wrong, True, "FEMALE_BREAST_EXPOSED")
    assert seg["marked_at"]
    assert seg["poster_url"].startswith("/api/plex-image")


async def test_empty_when_nothing_is_marked(http_client):
    resp = await http_client.get("/api/analytics/false-positives")
    assert resp.status_code == 200
    assert resp.json() == {"segments": [], "total": 0}


async def test_rejects_bad_paging(http_client):
    assert (await http_client.get("/api/analytics/false-positives?limit=0")).status_code == 422
    assert (await http_client.get("/api/analytics/false-positives?offset=-1")).status_code == 422
