"""Integration tests for sessions API routes."""

from __future__ import annotations

import collections
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cleanplex import database as db
from cleanplex.plex_client import ActiveSession
from tests.conftest import make_mock_plex_client


pytestmark = pytest.mark.usefixtures("setup_db")


def _active_session(
    *,
    session_key: str = "s1",
    user: str = "alice",
    title: str = "Movie",
    plex_guid: str = "g1",
    is_controllable: bool = True,
    position_ms: int = 5000,
    thumb: str = "",
) -> ActiveSession:
    return ActiveSession(
        session_key=session_key,
        user=user,
        title=title,
        full_title=title,
        plex_guid=plex_guid,
        rating_key="100",
        media_type="movie",
        position_ms=position_ms,
        duration_ms=7200000,
        client_identifier="client-1",
        client_title="Plex Web",
        is_controllable=is_controllable,
        thumb=thumb,
    )


# ── GET /api/sessions ─────────────────────────────────────────────────────────

async def test_get_sessions_returns_empty_when_no_plex(http_client):
    with patch("cleanplex.web.routes.sessions.plex_mod.get_client", side_effect=RuntimeError("not set")):
        resp = await http_client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["sessions"] == []
    assert "error" in data


async def test_get_sessions_returns_session_list(http_client):
    sessions = [_active_session()]
    mock_client = make_mock_plex_client(sessions=sessions)
    with patch("cleanplex.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.get("/api/sessions")
    assert resp.status_code == 200
    result = resp.json()["sessions"]
    assert len(result) == 1
    assert result[0]["session_key"] == "s1"
    assert result[0]["user"] == "alice"


async def test_get_sessions_filtering_enabled_default_when_no_filter(http_client):
    """Filtering is ON by default for users with no explicit filter record."""
    sessions = [_active_session(user="bob")]
    mock_client = make_mock_plex_client(sessions=sessions)
    with patch("cleanplex.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.get("/api/sessions")
    result = resp.json()["sessions"]
    assert result[0]["filtering_enabled"] is True


async def test_get_sessions_filtering_disabled_when_filter_set(http_client):
    await db.upsert_user_filter("charlie", enabled=False)
    sessions = [_active_session(user="charlie")]
    mock_client = make_mock_plex_client(sessions=sessions)
    with patch("cleanplex.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.get("/api/sessions")
    result = resp.json()["sessions"]
    assert result[0]["filtering_enabled"] is False


async def test_get_sessions_batches_user_filter_lookup(http_client):
    """All user filter lookups happen in one batch call, not N per session."""
    sessions = [_active_session(session_key=f"s{i}", user=f"user{i}") for i in range(5)]
    mock_client = make_mock_plex_client(sessions=sessions)

    with patch("cleanplex.web.routes.sessions.plex_mod.get_client", return_value=mock_client), \
         patch("cleanplex.web.routes.sessions.db.get_all_user_filters", wraps=db.get_all_user_filters) as spy:
        resp = await http_client.get("/api/sessions")

    assert resp.status_code == 200
    # Must call get_all_user_filters exactly once regardless of session count
    spy.assert_awaited_once()


# ── GET /api/sessions/events ──────────────────────────────────────────────────

async def test_get_skip_events_returns_empty_by_default(http_client):
    with patch("cleanplex.web.routes.sessions.skip_events", collections.deque()):
        resp = await http_client.get("/api/sessions/events")
    assert resp.status_code == 200
    assert resp.json()["events"] == []


async def test_get_skip_events_returns_events(http_client):
    events = [{"time": "2025-01-01 10:00:00", "user": "alice", "title": "Movie",
               "position_ms": 5000, "client": "Web"}]
    with patch("cleanplex.web.routes.sessions.skip_events", collections.deque(events)):
        resp = await http_client.get("/api/sessions/events")
    result = resp.json()["events"]
    assert len(result) == 1
    assert result[0]["user"] == "alice"


# ── GET /api/sessions/scanner-status ─────────────────────────────────────────

async def test_scanner_status_returns_expected_shape(http_client):
    with patch("cleanplex.web.routes.sessions.get_current_scan", return_value=None), \
         patch("cleanplex.web.routes.sessions.get_current_scans", return_value=[]), \
         patch("cleanplex.web.routes.sessions.get_queue_size", return_value=0), \
         patch("cleanplex.web.routes.sessions.get_worker_pool_size", return_value=2), \
         patch("cleanplex.web.routes.sessions.is_paused", return_value=False):
        resp = await http_client.get("/api/sessions/scanner-status")
    assert resp.status_code == 200
    data = resp.json()
    assert "queue_size" in data
    assert "active_scans" in data
    assert "paused" in data
    assert data["paused"] is False


async def test_scanner_status_batches_db_lookup(http_client):
    """scanner-status must call get_scan_jobs_by_guids (batch), not per-guid."""
    guid = "scan-guid-1"
    await db.upsert_scan_job(
        plex_guid=guid, title="Scanning Title", file_path="/f.mkv",
        rating_key="1", library_id="1", library_title="L",
    )
    await db.update_scan_job_status(guid, "scanning", progress=0.5)

    with patch("cleanplex.web.routes.sessions.get_current_scan", return_value=guid), \
         patch("cleanplex.web.routes.sessions.get_current_scans", return_value=[guid]), \
         patch("cleanplex.web.routes.sessions.get_queue_size", return_value=0), \
         patch("cleanplex.web.routes.sessions.get_worker_pool_size", return_value=2), \
         patch("cleanplex.web.routes.sessions.is_paused", return_value=False), \
         patch("cleanplex.web.routes.sessions.db.get_scan_jobs_by_guids",
               wraps=db.get_scan_jobs_by_guids) as spy:
        resp = await http_client.get("/api/sessions/scanner-status")

    assert resp.status_code == 200
    # Exactly one batch call, not a loop of individual queries
    spy.assert_awaited_once()
    data = resp.json()
    assert len(data["active_scans"]) == 1
    assert data["active_scans"][0]["guid"] == guid


# ── POST /api/sessions/{session_key}/skip ─────────────────────────────────────

async def test_skip_session_returns_404_when_plex_not_configured(http_client):
    with patch("cleanplex.web.routes.sessions.plex_mod.get_client", side_effect=RuntimeError):
        resp = await http_client.post("/api/sessions/s1/skip")
    assert resp.status_code == 503


async def test_skip_session_returns_404_when_session_not_found(http_client):
    mock_client = make_mock_plex_client(sessions=[])
    with patch("cleanplex.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.post("/api/sessions/no-such-session/skip")
    assert resp.status_code == 404


async def test_skip_session_returns_409_when_not_controllable(http_client):
    sessions = [_active_session(is_controllable=False)]
    mock_client = make_mock_plex_client(sessions=sessions)
    with patch("cleanplex.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.post("/api/sessions/s1/skip")
    assert resp.status_code == 409


async def test_skip_session_returns_404_when_no_segments(http_client):
    sessions = [_active_session()]
    mock_client = make_mock_plex_client(sessions=sessions)
    with patch("cleanplex.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.post("/api/sessions/s1/skip")
    assert resp.status_code == 404


async def test_skip_session_seeks_to_next_segment(http_client):
    await db.insert_segment("g1", "Movie", start_ms=30000, end_ms=60000, confidence=0.9)
    sessions = [_active_session(position_ms=10000)]
    mock_client = make_mock_plex_client(sessions=sessions, seek_result=True)
    with patch("cleanplex.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.post("/api/sessions/s1/skip")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["seek_to_ms"] == 25000  # 30000 - 5000 (expansion)
