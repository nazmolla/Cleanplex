"""Integration tests for segment-related API routes."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from cleanplex import database as db
from tests.conftest import make_mock_plex_client


pytestmark = pytest.mark.usefixtures("setup_db")


# ── GET /api/titles/{plex_guid}/segments ──────────────────────────────────────

async def test_get_segments_for_title_returns_empty(http_client):
    resp = await http_client.get("/api/titles/unknown-guid/segments")
    assert resp.status_code == 200
    assert resp.json()["segments"] == []


async def test_get_segments_for_title_returns_rows(http_client):
    await db.insert_segment("guid-seg", "Movie", start_ms=1000, end_ms=5000, confidence=0.9, labels="NUDITY")
    resp = await http_client.get("/api/titles/guid-seg/segments")
    assert resp.status_code == 200
    segs = resp.json()["segments"]
    assert len(segs) == 1
    assert segs[0]["start_ms"] == 1000
    assert segs[0]["end_ms"] == 5000


async def test_get_segments_for_title_has_thumbnail_url_when_path_set(http_client):
    seg_id = await db.insert_segment(
        "guid-thumb", "T", start_ms=0, end_ms=1000, thumbnail_path="/some/path.jpg"
    )
    resp = await http_client.get("/api/titles/guid-thumb/segments")
    segs = resp.json()["segments"]
    assert segs[0]["has_thumbnail"] is True
    assert f"/api/thumbnails/{seg_id}" in segs[0]["thumbnail_url"]


# ── DELETE /api/segments/{segment_id} ─────────────────────────────────────────

async def test_delete_segment_returns_ok(http_client):
    seg_id = await db.insert_segment("guid-del", "M", start_ms=0, end_ms=1000)
    with patch("cleanplex.web.routes.segments._refresh_cleanplex_summary_for_guid"):
        resp = await http_client.delete(f"/api/segments/{seg_id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_delete_segment_returns_404_for_missing(http_client):
    with patch("cleanplex.web.routes.segments._refresh_cleanplex_summary_for_guid"):
        resp = await http_client.delete("/api/segments/99999")
    assert resp.status_code == 404


async def test_delete_segment_actually_removes_row(http_client):
    seg_id = await db.insert_segment("guid-del2", "M", start_ms=0, end_ms=1000)
    with patch("cleanplex.web.routes.segments._refresh_cleanplex_summary_for_guid"):
        await http_client.delete(f"/api/segments/{seg_id}")
    assert await db.get_segment_by_id(seg_id) is None


# ── DELETE /api/titles/{plex_guid}/segments ───────────────────────────────────

async def test_delete_all_segments_for_title(http_client):
    await db.insert_segment("guid-bulk", "M", start_ms=0, end_ms=1000)
    await db.insert_segment("guid-bulk", "M", start_ms=2000, end_ms=3000)
    with patch("cleanplex.web.routes.segments._refresh_cleanplex_summary_for_guid"):
        resp = await http_client.delete("/api/titles/guid-bulk/segments")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 2
    assert await db.get_segments_for_guid("guid-bulk") == []


async def test_delete_all_segments_returns_zero_when_none(http_client):
    with patch("cleanplex.web.routes.segments._refresh_cleanplex_summary_for_guid"):
        resp = await http_client.delete("/api/titles/nonexistent/segments")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 0


# ── GET /api/segments ─────────────────────────────────────────────────────────

async def test_get_all_segments_empty(http_client):
    resp = await http_client.get("/api/segments")
    assert resp.status_code == 200
    assert resp.json()["segments"] == []


async def test_get_all_segments_pagination(http_client):
    for i in range(5):
        await db.insert_segment(f"guid-page-{i}", "T", start_ms=i * 1000, end_ms=i * 1000 + 500)
    resp = await http_client.get("/api/segments?limit=3&offset=0")
    assert resp.status_code == 200
    assert len(resp.json()["segments"]) == 3


# ── GET /api/libraries ─────────────────────────────────────────────────────────

async def test_get_libraries_returns_error_when_plex_not_configured(http_client):
    with patch("cleanplex.web.routes.segments.plex_mod.get_client", side_effect=RuntimeError("not configured")):
        resp = await http_client.get("/api/libraries")
    assert resp.status_code == 200
    data = resp.json()
    assert data["libraries"] == []
    assert "error" in data


async def test_get_libraries_returns_sections(http_client):
    from cleanplex.plex_client import LibrarySection
    mock_client = make_mock_plex_client(
        library_sections=[LibrarySection("1", "Movies", "movie"), LibrarySection("2", "Shows", "show")]
    )
    with patch("cleanplex.web.routes.segments.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.get("/api/libraries")
    assert resp.status_code == 200
    libs = resp.json()["libraries"]
    assert len(libs) == 2
    assert libs[0]["id"] == "1"


# ── GET /api/libraries/{library_id}/titles ────────────────────────────────────

async def test_get_titles_in_library_returns_empty_for_no_jobs(http_client):
    with patch("cleanplex.web.routes.segments.plex_mod.get_client", side_effect=RuntimeError):
        resp = await http_client.get("/api/libraries/lib1/titles")
    assert resp.status_code == 200
    assert resp.json()["titles"] == []


async def test_get_titles_in_library_returns_jobs(http_client):
    await db.upsert_scan_job(
        plex_guid="title-guid",
        title="Test Movie",
        file_path="/test.mkv",
        rating_key="1",
        library_id="lib1",
        library_title="Movies",
    )
    await db.insert_segment("title-guid", "Test Movie", start_ms=0, end_ms=1000)

    mock_client = make_mock_plex_client()
    with patch("cleanplex.web.routes.segments.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.get("/api/libraries/lib1/titles")

    assert resp.status_code == 200
    titles = resp.json()["titles"]
    assert len(titles) == 1
    assert titles[0]["plex_guid"] == "title-guid"
    assert titles[0]["segment_count"] == 1


# ── POST /api/libraries/{library_id}/sync ─────────────────────────────────────

async def test_sync_library_returns_error_when_plex_not_configured(http_client):
    with patch("cleanplex.web.routes.segments.plex_mod.get_client", side_effect=RuntimeError("not set")):
        resp = await http_client.post("/api/libraries/lib1/sync")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


async def test_sync_library_adds_new_titles(http_client):
    from cleanplex.plex_client import MediaItem
    items = [
        MediaItem(
            rating_key="100",
            plex_guid="sync-g1",
            title="New Movie",
            year=2024,
            thumb="/t",
            file_path="/new.mkv",
            library_id="lib2",
            library_title="Movies",
            media_type="movie",
            content_rating="R",
        )
    ]
    mock_client = make_mock_plex_client(library_items=items)
    with patch("cleanplex.web.routes.segments.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.post("/api/libraries/lib2/sync")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["new"] == 1
