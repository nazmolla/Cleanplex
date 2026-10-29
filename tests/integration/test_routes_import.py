"""Integration tests for the skip file import and export routes (issue #67)."""

from __future__ import annotations

import pytest

from cleanplex import database as db

pytestmark = pytest.mark.usefixtures("setup_db")

SKP_BODY = (
    "0:00:22.36\n"
    "Reference shot\n"
    "\n"
    "0:00:50.76 --> 0:01:01.18\n"
    "Nudity 2 (distant)\n"
    "\n"
    "0:26:23.5 --> 0:26:53.53\n"
    "profane word 1 (f-)\n"
)


async def _make_job(guid: str = "guid-import", duration_ms: int = 7200000) -> None:
    await db.upsert_scan_job(
        plex_guid=guid,
        title="Test Movie",
        file_path="/media/test.mkv",
        rating_key="rk-1",
        library_id="1",
        library_title="Movies",
        duration_ms=duration_ms,
    )


def _upload(body: str, filename: str, guid: str = "guid-import", replace: bool = False):
    return {
        "files": {"file": (filename, body.encode("utf-8"), "text/plain")},
        "data": {"plex_guid": guid, "replace": str(replace).lower()},
    }


# ── POST /api/segments/import ─────────────────────────────────────────────────

async def test_import_skp_creates_segments(http_client):
    await _make_job()

    resp = await http_client.post("/api/segments/import", **_upload(SKP_BODY, "movie.skp"))

    assert resp.status_code == 200
    assert resp.json()["imported"] == 2
    assert resp.json()["source"] == "skp"

    stored = await db.get_segments_for_guid("guid-import")
    assert {s["category"] for s in stored} == {"nudity", "language"}
    assert all(s["source"] == "skp" for s in stored)


async def test_import_edl_creates_segments(http_client):
    await _make_job()

    resp = await http_client.post("/api/segments/import", **_upload("30.5 45.0 0\n", "movie.edl"))

    assert resp.status_code == 200
    stored = await db.get_segments_for_guid("guid-import")
    assert stored[0]["start_ms"] == 30500
    assert stored[0]["source"] == "edl"


async def test_import_rejects_unparseable_file_and_stores_nothing(http_client):
    await _make_job()

    resp = await http_client.post("/api/segments/import", **_upload("not a skip file", "movie.skp"))

    assert resp.status_code == 422
    assert "No skip cues" in resp.json()["detail"]
    assert await db.get_segments_for_guid("guid-import") == []


async def test_import_rejects_unsupported_extension(http_client):
    await _make_job()

    resp = await http_client.post("/api/segments/import", **_upload("whatever", "movie.srt"))

    assert resp.status_code == 422
    assert "Unsupported" in resp.json()["detail"]


async def test_import_for_unknown_guid_returns_404(http_client):
    resp = await http_client.post(
        "/api/segments/import", **_upload(SKP_BODY, "movie.skp", guid="nope")
    )

    assert resp.status_code == 404


async def test_import_appends_by_default_and_replaces_when_asked(http_client):
    await _make_job()
    await db.insert_segment("guid-import", "Test Movie", 1000, 2000)

    await http_client.post("/api/segments/import", **_upload(SKP_BODY, "movie.skp"))
    assert len(await db.get_segments_for_guid("guid-import")) == 3

    await http_client.post("/api/segments/import", **_upload(SKP_BODY, "movie.skp", replace=True))
    assert len(await db.get_segments_for_guid("guid-import")) == 2


async def test_import_flags_a_runtime_mismatch(http_client):
    """Timings past the end of the media mean a different cut of the film."""
    await _make_job(duration_ms=60000)

    resp = await http_client.post("/api/segments/import", **_upload(SKP_BODY, "movie.skp"))

    assert resp.status_code == 200
    assert "different release" in resp.json()["warning"]


async def test_import_has_no_warning_when_runtime_matches(http_client):
    await _make_job(duration_ms=7200000)

    resp = await http_client.post("/api/segments/import", **_upload(SKP_BODY, "movie.skp"))

    assert resp.json()["warning"] is None


# ── POST /api/segments/import/paste ───────────────────────────────────────────

async def test_paste_import_creates_segments(http_client):
    await _make_job()

    resp = await http_client.post(
        "/api/segments/import/paste",
        json={"plex_guid": "guid-import", "text": "00:12:30 - 00:13:05 nudity\n"},
    )

    assert resp.status_code == 200
    assert resp.json()["imported"] == 1
    stored = await db.get_segments_for_guid("guid-import")
    assert stored[0]["category"] == "nudity"
    assert stored[0]["source"] == "paste"


async def test_paste_import_rejects_prose_only_input(http_client):
    await _make_job()

    resp = await http_client.post(
        "/api/segments/import/paste",
        json={"plex_guid": "guid-import", "text": "there are no timestamps here"},
    )

    assert resp.status_code == 422
    assert "No timestamp ranges" in resp.json()["detail"]


async def test_paste_import_requires_plex_guid(http_client):
    resp = await http_client.post("/api/segments/import/paste", json={"text": "00:01 - 00:02"})

    assert resp.status_code == 422


# ── GET /api/segments/export/{plex_guid} ──────────────────────────────────────

async def test_export_edl_renders_rows(http_client):
    await _make_job()
    await db.insert_segment("guid-import", "Test Movie", 30500, 45000)

    resp = await http_client.get("/api/segments/export/guid-import?fmt=edl")

    assert resp.status_code == 200
    assert resp.json()["body"].startswith("30.500\t45.000\t0")


async def test_export_mcf_renders_a_valid_document(http_client):
    from cleanplex.importers import mcf

    await _make_job()
    await db.insert_segment("guid-import", "Test Movie", 30500, 45000, category="violence")

    resp = await http_client.get("/api/segments/export/guid-import?fmt=mcf")
    body = resp.json()["body"]

    assert body.startswith("WEBVTT MovieContentFilter 1.1.0")
    assert mcf.parse(body)[0]["category"] == "violence"


async def test_export_unknown_format_returns_422(http_client):
    await _make_job()

    resp = await http_client.get("/api/segments/export/guid-import?fmt=banana")

    assert resp.status_code == 422


async def test_export_unknown_guid_returns_404(http_client):
    resp = await http_client.get("/api/segments/export/nope?fmt=edl")

    assert resp.status_code == 404


# ── Segment API exposes classification ────────────────────────────────────────

async def test_title_segments_expose_classification(http_client):
    await _make_job()
    await http_client.post("/api/segments/import", **_upload(SKP_BODY, "movie.skp"))

    segs = (await http_client.get("/api/titles/guid-import/segments")).json()["segments"]
    by_category = {s["category"]: s for s in segs}

    assert by_category["language"]["action"] == "mute"
    assert by_category["language"]["source"] == "skp"
    assert by_category["nudity"]["severity"] == "medium"


async def test_scanner_segments_report_scanner_defaults(http_client):
    await _make_job()
    await db.insert_segment("guid-import", "Test Movie", 1000, 2000)

    segs = (await http_client.get("/api/titles/guid-import/segments")).json()["segments"]

    assert segs[0]["category"] == "nudity"
    assert segs[0]["source"] == "scanner"
    assert segs[0]["action"] == "skip"


async def test_export_handles_guids_containing_slashes(http_client):
    """Plex GUIDs look like imdb://tt123 — the route needs the path converter."""
    await _make_job(guid="imdb://tt32897959")
    await db.insert_segment("imdb://tt32897959", "Test Movie", 30500, 45000)

    resp = await http_client.get("/api/segments/export/imdb://tt32897959?fmt=edl")

    assert resp.status_code == 200
    assert resp.json()["count"] == 1


async def test_import_handles_guids_containing_slashes(http_client):
    await _make_job(guid="imdb://tt32897959")

    resp = await http_client.post(
        "/api/segments/import", **_upload(SKP_BODY, "movie.skp", guid="imdb://tt32897959")
    )

    assert resp.status_code == 200
    assert resp.json()["imported"] == 2


async def test_importing_refreshes_the_plex_summary(http_client):
    from unittest.mock import patch
    await db.upsert_scan_job(plex_guid="g-sum", title="T", file_path="/m.mkv", rating_key="9",
                             library_id="l", library_title="L")
    with patch("cleanplex.web.routes.import_routes.plex_summary.refresh_soon") as refresh:
        resp = await http_client.post("/api/segments/import/paste", json={
            "plex_guid": "g-sum", "text": "00:01:00 - 00:01:30 nudity", "replace": False})
    assert resp.status_code == 200
    refresh.assert_called_once_with("g-sum")
