"""Integration tests for scanner API routes."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from cleanplex import database as db


pytestmark = pytest.mark.usefixtures("setup_db")


async def _make_job(guid: str = "job-guid", title: str = "Movie", library_id: str = "lib1"):
    await db.upsert_scan_job(
        plex_guid=guid,
        title=title,
        file_path=f"/media/{guid}.mkv",
        rating_key="100",
        library_id=library_id,
        library_title="Movies",
    )


# ── GET /api/scan/queue ───────────────────────────────────────────────────────

async def test_get_scan_queue_returns_jobs(http_client):
    await _make_job()
    with patch("cleanplex.web.routes.scanner_routes.scan_mod.get_queue_size", return_value=0), \
         patch("cleanplex.web.routes.scanner_routes.scan_mod.get_current_scan", return_value=None), \
         patch("cleanplex.web.routes.scanner_routes.scan_mod.get_current_scans", return_value=[]), \
         patch("cleanplex.web.routes.scanner_routes.scan_mod.is_paused", return_value=False):
        resp = await http_client.get("/api/scan/queue")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["jobs"]) == 1
    assert data["jobs"][0]["plex_guid"] == "job-guid"


async def test_get_scan_queue_paused_field(http_client):
    with patch("cleanplex.web.routes.scanner_routes.scan_mod.get_queue_size", return_value=0), \
         patch("cleanplex.web.routes.scanner_routes.scan_mod.get_current_scan", return_value=None), \
         patch("cleanplex.web.routes.scanner_routes.scan_mod.get_current_scans", return_value=[]), \
         patch("cleanplex.web.routes.scanner_routes.scan_mod.is_paused", return_value=True):
        resp = await http_client.get("/api/scan/queue")
    assert resp.json()["paused"] is True


# ── POST /api/scan/title ──────────────────────────────────────────────────────

async def test_scan_title_returns_404_when_job_not_found(http_client):
    with patch("cleanplex.web.routes.scanner_routes.plex_mod.get_client", side_effect=RuntimeError):
        resp = await http_client.post("/api/scan/title", json={"plex_guid": "no-such-guid"})
    assert resp.status_code == 404


async def test_scan_title_queues_existing_job(http_client):
    await _make_job("scan-me")
    with patch("cleanplex.web.routes.scanner_routes.scan_mod.enqueue", new=AsyncMock()) as mock_enqueue, \
         patch("cleanplex.web.routes.scanner_routes.scan_mod.get_queue_size", return_value=1):
        resp = await http_client.post("/api/scan/title", json={"plex_guid": "scan-me"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["queued"] == "scan-me"
    mock_enqueue.assert_awaited_once_with("scan-me")


async def test_scan_title_force_calls_force_scan_job(http_client):
    await _make_job("force-me")
    with patch("cleanplex.web.routes.scanner_routes.scan_mod.force_scan_job", new=AsyncMock()) as mock_force, \
         patch("cleanplex.web.routes.scanner_routes.scan_mod.get_queue_size", return_value=0):
        resp = await http_client.post("/api/scan/title", json={"plex_guid": "force-me", "now": True})
    assert resp.status_code == 200
    mock_force.assert_awaited_once_with("force-me")


# ── POST /api/scan/pause ──────────────────────────────────────────────────────

async def test_pause_scanner(http_client):
    with patch("cleanplex.web.routes.scanner_routes.scan_mod.pause_scanner") as mock_pause:
        resp = await http_client.post("/api/scan/pause")
    assert resp.status_code == 200
    assert resp.json()["paused"] is True
    mock_pause.assert_called_once()


# ── POST /api/scan/resume ─────────────────────────────────────────────────────

async def test_resume_scanner(http_client):
    with patch("cleanplex.web.routes.scanner_routes.scan_mod.resume_scanner") as mock_resume:
        resp = await http_client.post("/api/scan/resume")
    assert resp.status_code == 200
    assert resp.json()["paused"] is False
    mock_resume.assert_called_once()


# ── POST /api/scan/skip-current ───────────────────────────────────────────────

async def test_skip_current_scan_returns_404_when_nothing_scanning(http_client):
    with patch("cleanplex.web.routes.scanner_routes.scan_mod.get_current_scan", return_value=None):
        resp = await http_client.post("/api/scan/skip-current", json={})
    assert resp.status_code == 404


async def test_skip_current_scan_returns_ok(http_client):
    with patch("cleanplex.web.routes.scanner_routes.scan_mod.get_current_scan", return_value="guid-curr"), \
         patch("cleanplex.web.routes.scanner_routes.scan_mod.skip_current_scan") as mock_skip:
        resp = await http_client.post("/api/scan/skip-current", json={})
    assert resp.status_code == 200
    assert resp.json()["skipped"] == "guid-curr"
    mock_skip.assert_called_once()


async def test_skip_current_scan_specific_guid(http_client):
    with patch("cleanplex.web.routes.scanner_routes.scan_mod.request_skip_scan", return_value=True) as mock_skip:
        resp = await http_client.post("/api/scan/skip-current", json={"plex_guid": "specific-guid"})
    assert resp.status_code == 200
    assert resp.json()["skipped"] == "specific-guid"
    mock_skip.assert_called_once_with("specific-guid")


async def test_skip_current_scan_specific_guid_not_active(http_client):
    with patch("cleanplex.web.routes.scanner_routes.scan_mod.request_skip_scan", return_value=False):
        resp = await http_client.post("/api/scan/skip-current", json={"plex_guid": "not-active"})
    assert resp.status_code == 404


# ── POST /api/scan/title/{plex_guid}/ignore ───────────────────────────────────

async def test_toggle_ignored_sets_flag(http_client):
    await _make_job("ig-guid")
    resp = await http_client.post("/api/scan/title/ig-guid/ignore", json={"ignored": True})
    assert resp.status_code == 200
    assert resp.json()["ignored"] is True
    job = await db.get_scan_job_by_guid("ig-guid")
    assert job["ignored"] == 1


async def test_toggle_ignored_clears_flag(http_client):
    await _make_job("ig-guid2")
    await db.set_ignored("ig-guid2", True)
    resp = await http_client.post("/api/scan/title/ig-guid2/ignore", json={"ignored": False})
    assert resp.status_code == 200
    job = await db.get_scan_job_by_guid("ig-guid2")
    assert job["ignored"] == 0


async def test_toggle_ignored_returns_404_for_missing(http_client):
    resp = await http_client.post("/api/scan/title/no-such/ignore", json={"ignored": True})
    assert resp.status_code == 404


# ── POST /api/scan/library/{library_id} ───────────────────────────────────────

async def test_scan_library_queues_pending_jobs(http_client):
    await _make_job("lib-job-1")
    await _make_job("lib-job-2")
    with patch("cleanplex.web.routes.scanner_routes.scan_mod.enqueue", new=AsyncMock()):
        resp = await http_client.post("/api/scan/library/lib1", json={})
    assert resp.status_code == 200
    assert resp.json()["queued"] == 2


async def test_scan_library_skips_done_jobs(http_client):
    await _make_job("done-job", library_id="lib2")
    await db.update_scan_job_status("done-job", "done")
    with patch("cleanplex.web.routes.scanner_routes.scan_mod.enqueue", new=AsyncMock()) as mock_enqueue:
        resp = await http_client.post("/api/scan/library/lib2", json={})
    assert resp.status_code == 200
    assert resp.json()["queued"] == 0
    mock_enqueue.assert_not_awaited()
