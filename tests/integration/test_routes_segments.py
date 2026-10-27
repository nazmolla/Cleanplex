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


# ── Preview clips and VLC playlist (issue #80) ────────────────────────────────

async def _segment_with_file(tmp_path, start_ms=60000, end_ms=70000):
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"not really video")
    await db.upsert_scan_job(plex_guid="guid-prev", title="Movie", file_path=str(media),
                             rating_key="1", library_id="lib", library_title="Lib")
    return await db.insert_segment("guid-prev", "Movie", start_ms=start_ms, end_ms=end_ms), media


@pytest.fixture
def fake_render(tmp_path, monkeypatch):
    """Replace ffmpeg with a recorder that writes a small fake clip."""
    import asyncio
    from types import SimpleNamespace
    from cleanplex.web.routes import segments as seg_routes

    monkeypatch.setattr(seg_routes, "PREVIEWS_DIR", tmp_path / "previews")
    state = SimpleNamespace(calls=[], ok=True)

    async def render(path, start_ms, duration_ms, out_path):
        state.calls.append((path, start_ms, duration_ms))
        await asyncio.sleep(0.05)  # long enough for concurrent requests to overlap
        if state.ok:
            out_path.write_bytes(b"0123456789" * 10)
        return state.ok

    monkeypatch.setattr(seg_routes.frame_extractor, "render_clip", render)
    return state


async def test_preview_renders_the_padded_clip_once_and_serves_it(http_client, tmp_path, fake_render):
    seg_id, media = await _segment_with_file(tmp_path)

    first = await http_client.get(f"/api/segments/{seg_id}/preview")
    second = await http_client.get(f"/api/segments/{seg_id}/preview")

    assert first.status_code == second.status_code == 200
    assert first.headers["content-type"] == "video/mp4"
    assert first.headers["x-clip-start-ms"] == "55000"
    assert first.content == b"0123456789" * 10
    assert fake_render.calls == [(str(media), 55000, 20000)]


async def test_preview_answers_byte_range_requests_for_safari(http_client, tmp_path, fake_render):
    """Regression for #93: Safari plays nothing unless a range request gets a 206."""
    seg_id, _ = await _segment_with_file(tmp_path)

    resp = await http_client.get(f"/api/segments/{seg_id}/preview", headers={"Range": "bytes=0-1"})

    assert resp.status_code == 206
    assert resp.headers["content-range"] == "bytes 0-1/100"
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.content == b"01"


async def test_concurrent_first_requests_render_once(http_client, tmp_path, fake_render):
    import asyncio

    seg_id, _ = await _segment_with_file(tmp_path)

    results = await asyncio.gather(*(http_client.get(f"/api/segments/{seg_id}/preview") for _ in range(3)))

    assert [r.status_code for r in results] == [200, 200, 200]
    assert len(fake_render.calls) == 1


async def test_preview_returns_502_when_rendering_fails(http_client, tmp_path, fake_render):
    seg_id, _ = await _segment_with_file(tmp_path)
    fake_render.ok = False

    assert (await http_client.get(f"/api/segments/{seg_id}/preview")).status_code == 502


async def test_preview_clamps_padding_at_the_start_and_caps_length(http_client, tmp_path, fake_render):
    seg_id, _ = await _segment_with_file(tmp_path, start_ms=2000, end_ms=2 * 60 * 60 * 1000)

    await http_client.get(f"/api/segments/{seg_id}/preview")

    assert [(s, d) for _, s, d in fake_render.calls] == [(0, 10 * 60 * 1000)]


async def test_preview_404_for_unknown_segment(http_client):
    assert (await http_client.get("/api/segments/99999/preview")).status_code == 404


async def test_preview_404_when_source_file_is_missing(http_client, tmp_path):
    seg_id, media = await _segment_with_file(tmp_path)
    media.unlink()

    assert (await http_client.get(f"/api/segments/{seg_id}/preview")).status_code == 404


async def test_vlc_playlist_starts_and_stops_around_the_segment(http_client, tmp_path):
    seg_id, _ = await _segment_with_file(tmp_path)

    body = (await http_client.get(f"/api/segments/{seg_id}/vlc")).text

    assert "#EXTVLCOPT:start-time=55.0" in body
    assert "#EXTVLCOPT:stop-time=75.0" in body
    assert body.strip().endswith(f"/api/segments/{seg_id}/stream")


async def test_segment_lists_are_never_browser_cached(http_client):
    """Regression for #81: a cached list hid rescan results until a hard reload."""
    await db.insert_segment("guid-nc", "Movie", start_ms=0, end_ms=1000)

    single = await http_client.get("/api/titles/guid-nc/segments")
    batch = await http_client.post("/api/titles/segments/batch", json={"guids": ["guid-nc"]})

    assert single.headers["cache-control"] == "no-store"
    assert batch.headers["cache-control"] == "no-store"


async def test_segment_list_reports_its_detector(http_client):
    """#92: the UI labels each segment with the model that found it."""
    await db.insert_segment("guid-det", "Movie", start_ms=0, end_ms=1000, detector="nudenet-640m")
    await db.insert_segment("guid-det", "Movie", start_ms=2000, end_ms=3000, source="skp")

    segs = (await http_client.get("/api/titles/guid-det/segments")).json()["segments"]

    assert [(s["source"], s["detector"]) for s in segs] == [("scanner", "nudenet-640m"), ("skp", "")]
