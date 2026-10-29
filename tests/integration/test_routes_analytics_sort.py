"""Sorting of the Analytics segment lists by score and date added (#128)."""

from __future__ import annotations

import pytest

from cleanplex import database as db

pytestmark = pytest.mark.usefixtures("setup_db")

LABEL = "BUTTOCKS_EXPOSED"


async def _seed(false_positive: bool = False):
    """Three segments: scores 0.9/0.5/0.7 added on days 2/3/1 (oldest first = id c)."""
    await db.upsert_scan_job(plex_guid="g", title="Movie", file_path="/m.mkv", rating_key="7",
                             library_id="lib", library_title="Movies")
    ids = {}
    for name, score, added in (("a", 0.9, "2026-10-02"), ("b", 0.5, "2026-10-03"), ("c", 0.7, "2026-10-01")):
        ids[name] = await db.insert_segment("g", "Movie", len(ids) * 10_000, len(ids) * 10_000 + 1000,
                                            confidence=score, labels=LABEL)
        async with db.get_connection() as conn:
            await conn.execute("UPDATE segments SET created_at=? WHERE id=?", (added, ids[name]))
            await conn.commit()
    if false_positive:
        for name in ("a", "b", "c"):
            await db.mark_segment_false_positive(ids[name])
        async with db.get_connection() as conn:
            for name, marked in (("a", "2026-10-20"), ("b", "2026-10-10"), ("c", "2026-10-30")):
                await conn.execute("UPDATE segment_feedback SET created_at=? WHERE segment_id=?",
                                   (marked, ids[name]))
            await conn.commit()
    return ids


async def _order(http_client, path):
    resp = await http_client.get(path)
    assert resp.status_code == 200
    return [s["id"] for s in resp.json()["segments"]]


async def test_label_list_defaults_to_score_descending(http_client):
    ids = await _seed()
    assert await _order(http_client, f"/api/analytics/segments?labels={LABEL}") == [ids["a"], ids["c"], ids["b"]]


async def test_label_list_sorts_by_score_ascending(http_client):
    ids = await _seed()
    got = await _order(http_client, f"/api/analytics/segments?labels={LABEL}&sort=score&order=asc")
    assert got == [ids["b"], ids["c"], ids["a"]]


async def test_label_list_sorts_by_date_added_both_ways(http_client):
    ids = await _seed()
    newest = await _order(http_client, f"/api/analytics/segments?labels={LABEL}&sort=added&order=desc")
    oldest = await _order(http_client, f"/api/analytics/segments?labels={LABEL}&sort=added&order=asc")
    assert newest == [ids["b"], ids["a"], ids["c"]]
    assert oldest == [ids["c"], ids["a"], ids["b"]]


async def test_paging_under_a_sort_has_no_gaps_or_repeats(http_client):
    ids = await _seed()
    base = f"/api/analytics/segments?labels={LABEL}&sort=score&order=asc&limit=1"
    pages = [await _order(http_client, f"{base}&offset={i}") for i in range(3)]
    assert [p[0] for p in pages] == [ids["b"], ids["c"], ids["a"]]


async def test_equal_scores_keep_a_stable_order(http_client):
    await db.upsert_scan_job(plex_guid="g", title="M", file_path="/m.mkv", rating_key="7",
                             library_id="lib", library_title="Movies")
    ids = [await db.insert_segment("g", "M", i * 10_000, i * 10_000 + 1000, confidence=0.6, labels=LABEL)
           for i in range(3)]
    desc = await _order(http_client, f"/api/analytics/segments?labels={LABEL}&order=desc")
    asc = await _order(http_client, f"/api/analytics/segments?labels={LABEL}&order=asc")
    assert desc == sorted(ids, reverse=True) and asc == sorted(ids)


@pytest.mark.parametrize("query", ["sort=bogus", "order=sideways", "sort=marked"])
async def test_label_list_rejects_unknown_sort_or_order(http_client, query):
    resp = await http_client.get(f"/api/analytics/segments?labels={LABEL}&{query}")
    assert resp.status_code == 422


async def test_false_positives_default_to_newest_mark_first(http_client):
    ids = await _seed(false_positive=True)
    assert await _order(http_client, "/api/analytics/false-positives") == [ids["c"], ids["a"], ids["b"]]


async def test_false_positives_sort_by_score_and_date_added(http_client):
    ids = await _seed(false_positive=True)
    assert await _order(http_client, "/api/analytics/false-positives?sort=score&order=asc") == [ids["b"], ids["c"], ids["a"]]
    assert await _order(http_client, "/api/analytics/false-positives?sort=added&order=desc") == [ids["b"], ids["a"], ids["c"]]
    assert await _order(http_client, "/api/analytics/false-positives?sort=marked&order=asc") == [ids["b"], ids["a"], ids["c"]]


async def test_false_positives_reject_unknown_sort(http_client):
    assert (await http_client.get("/api/analytics/false-positives?sort=bogus")).status_code == 422


async def test_db_rejects_unwhitelisted_sort_before_building_sql():
    with pytest.raises(ValueError):
        await db.get_segments_for_labels([LABEL], sort="confidence; DROP TABLE segments")
    with pytest.raises(ValueError):
        await db.get_false_positive_segments(10, 0, sort="marked", order="sideways")


async def test_analytics_rows_carry_the_thumbnail_time(http_client):
    await db.upsert_scan_job(plex_guid="g", title="T", file_path="/m.mkv", rating_key="7",
                             library_id="l", library_title="L")
    await db.insert_segment("g", "T", 10_000, 20_000, labels=LABEL, thumbnail_ms=14_000)

    resp = await http_client.get(f"/api/analytics/segments?labels={LABEL}")

    assert resp.json()["segments"][0]["thumbnail_ms"] == 14_000
