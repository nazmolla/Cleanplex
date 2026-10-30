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
    assert resp.json() == {"segments": [], "total": 0, "counts": {"marked": 0, "verifier": 0}}


async def test_rejects_bad_paging(http_client):
    assert (await http_client.get("/api/analytics/false-positives?limit=0")).status_code == 422
    assert (await http_client.get("/api/analytics/false-positives?offset=-1")).status_code == 422


async def test_kind_filter_separates_user_marks_from_verifier_rejections(http_client):
    await db.upsert_scan_job("g", "T", "/m.mkv", "1", "l", "L")
    mine = await db.insert_segment("g", "T", 0, 500, 0.9)
    await db.mark_segment_false_positive(mine, "")
    auto = await db.insert_segment("g", "T", 90_000, 90_500, 0.9, false_positive=db.FP_VERIFIER)

    everything = (await http_client.get("/api/analytics/false-positives")).json()
    only_auto = (await http_client.get("/api/analytics/false-positives?kind=verifier")).json()
    only_mine = (await http_client.get("/api/analytics/false-positives?kind=marked")).json()

    assert everything["total"] == 2 and everything["counts"] == {"marked": 1, "verifier": 1}
    assert [s["id"] for s in only_auto["segments"]] == [auto] and only_auto["segments"][0]["auto_rejected"] is True
    assert [s["id"] for s in only_mine["segments"]] == [mine] and only_mine["segments"][0]["auto_rejected"] is False


async def test_detection_accuracy_leaves_verifier_rejections_out_of_the_rate(http_client):
    await db.upsert_scan_job("g", "T", "/m.mkv", "1", "l", "L")
    await db.insert_segment("g", "T", 0, 500, 0.9, labels="BUTTOCKS_EXPOSED", detector="nudenet-320n")
    bad = await db.insert_segment("g", "T", 90_000, 90_500, 0.9, labels="BUTTOCKS_EXPOSED", detector="nudenet-320n")
    await db.mark_segment_false_positive(bad, "")
    await db.insert_segment("g", "T", 190_000, 190_500, 0.9, labels="BUTTOCKS_EXPOSED", detector="nudenet-320n",
                            false_positive=db.FP_VERIFIER)

    row = (await http_client.get("/api/analytics/detection-accuracy")).json()["rows"][0]

    assert (row["total"], row["false_positives"], row["verifier_rejected"], row["false_positive_rate"]) == (3, 1, 1, 0.5)
