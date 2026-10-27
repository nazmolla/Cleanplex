"""Integration tests for skip history analytics routes (issue #70)."""

from __future__ import annotations

import pytest

from cleanplex import database as db

pytestmark = pytest.mark.usefixtures("setup_db")


async def _seed() -> None:
    await db.record_skip_event(
        plex_guid="guid-a", title="Movie A", username="alice",
        client_identifier="c1", client_title="Apple TV",
        category="nudity", action="skip", latency_ms=120, success=True,
    )
    await db.record_skip_event(
        plex_guid="guid-a", title="Movie A", username="alice",
        client_identifier="c2", client_title="Roku",
        category="language", action="mute", latency_ms=900, success=False,
    )


async def test_by_category_returns_counts(http_client):
    await _seed()

    resp = await http_client.get("/api/analytics/skips/by-category")

    assert resp.status_code == 200
    categories = {c["category"]: c["count"] for c in resp.json()["categories"]}
    assert categories == {"nudity": 1, "language": 1}


async def test_by_client_reports_failure_rate_and_latency(http_client):
    await _seed()

    resp = await http_client.get("/api/analytics/skips/by-client")
    clients = resp.json()["clients"]

    assert resp.status_code == 200
    assert clients[0]["client_title"] == "Roku"
    assert clients[0]["failure_rate"] == 1.0
    assert clients[0]["avg_latency_ms"] == 900


async def test_top_titles_ranks_by_volume(http_client):
    await _seed()

    resp = await http_client.get("/api/analytics/skips/top-titles")

    assert resp.json()["titles"][0]["title"] == "Movie A"
    assert resp.json()["titles"][0]["count"] == 2


async def test_history_returns_events_newest_first(http_client):
    await _seed()

    resp = await http_client.get("/api/analytics/skips/history?limit=1")
    body = resp.json()

    assert resp.status_code == 200
    assert body["total"] == 2
    assert len(body["events"]) == 1


async def test_history_rejects_an_out_of_range_limit(http_client):
    resp = await http_client.get("/api/analytics/skips/history?limit=9999")

    assert resp.status_code == 422


async def test_empty_history_returns_empty_aggregates(http_client):
    resp = await http_client.get("/api/analytics/skips/by-category")

    assert resp.status_code == 200
    assert resp.json()["categories"] == []


# ── Date-filtered history (issue #78) ──────────────────────────────────────────

async def _seed_dated() -> None:
    for when in ("2026-10-01 10:00:00", "2026-10-02 10:00:00", "2026-10-03 23:30:00"):
        row_id = await db.record_skip_event(plex_guid="g", title=when)
        async with db.get_connection() as conn:
            await conn.execute("UPDATE skip_events SET created_at=? WHERE id=?", (when, row_id))
            await conn.commit()


async def test_history_without_dates_returns_everything(http_client):
    await _seed_dated()

    body = (await http_client.get("/api/analytics/skips/history")).json()

    assert body["total"] == 3
    assert [e["title"][:10] for e in body["events"]] == ["2026-10-03", "2026-10-02", "2026-10-01"]


async def test_history_date_range_is_start_inclusive_end_exclusive(http_client):
    await _seed_dated()

    resp = await http_client.get(
        "/api/analytics/skips/history?start=2026-10-02T00:00:00Z&end=2026-10-03T00:00:00Z"
    )
    body = resp.json()

    assert resp.status_code == 200
    assert body["total"] == 1
    assert body["events"][0]["title"].startswith("2026-10-02")


async def test_history_converts_offset_bounds_to_utc(http_client):
    await _seed_dated()

    # Local midnight Oct 4 at UTC-4 is 04:00 UTC, so the 23:30 UTC event on Oct 3 is in range.
    resp = await http_client.get(
        "/api/analytics/skips/history?start=2026-10-03T00:00:00-04:00&end=2026-10-04T00:00:00-04:00"
    )

    assert resp.json()["total"] == 1


async def test_history_rejects_a_malformed_date(http_client):
    resp = await http_client.get("/api/analytics/skips/history?start=not-a-date")

    assert resp.status_code == 422
