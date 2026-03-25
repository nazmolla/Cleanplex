"""Integration tests for settings, users, and thumbnails API routes."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cleanplex import database as db
from tests.conftest import make_mock_plex_client


pytestmark = pytest.mark.usefixtures("setup_db")


# ── GET /api/settings ─────────────────────────────────────────────────────────

async def test_get_settings_returns_dict(http_client):
    resp = await http_client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    # Route returns the settings dict directly (not wrapped in {"settings": ...})
    assert "plex_url" in data
    assert "confidence_threshold" in data


async def test_get_settings_reflects_stored_values(http_client):
    await db.set_setting("plex_url", "http://testplex:32400")
    resp = await http_client.get("/api/settings")
    assert resp.json()["plex_url"] == "http://testplex:32400"


# ── PUT /api/settings ─────────────────────────────────────────────────────────

async def test_update_settings_writes_values(http_client):
    with patch("cleanplex.web.routes.settings.scan_mod.request_scanner_restart", new=AsyncMock()):
        resp = await http_client.put("/api/settings", json={"log_level": "DEBUG"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert await db.get_setting("log_level") == "DEBUG"


async def test_update_settings_ignores_none_fields(http_client):
    await db.set_setting("poll_interval", "5")
    with patch("cleanplex.web.routes.settings.scan_mod.request_scanner_restart", new=AsyncMock()):
        resp = await http_client.put("/api/settings", json={"log_level": "WARNING"})
    assert resp.status_code == 200
    # poll_interval not included in payload — should remain unchanged
    assert await db.get_setting("poll_interval") == "5"


async def test_update_settings_reinitialises_plex_client_on_url_change(http_client):
    await db.set_setting("plex_token", "token-abc")
    with patch("cleanplex.web.routes.settings.plex_mod.init_client") as mock_init, \
         patch("cleanplex.web.routes.settings.scan_mod.request_scanner_restart", new=AsyncMock()):
        resp = await http_client.put("/api/settings", json={"plex_url": "http://newplex:32400"})
    assert resp.status_code == 200
    mock_init.assert_called_once()


async def test_update_settings_triggers_scanner_restart_on_worker_change(http_client):
    await db.set_setting("scan_workers", "2")
    with patch("cleanplex.web.routes.settings.scan_mod.request_scanner_restart", new=AsyncMock()) as mock_restart:
        resp = await http_client.put("/api/settings", json={"scan_workers": "4"})
    assert resp.status_code == 200
    mock_restart.assert_awaited_once()


# ── POST /api/settings/test-connection ────────────────────────────────────────

async def test_test_connection_requires_url_and_token(http_client):
    resp = await http_client.post("/api/settings/test-connection")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "required" in data["message"].lower()


async def test_test_connection_returns_ok_true(http_client):
    await db.set_setting("plex_url", "http://plex:32400")
    await db.set_setting("plex_token", "tok")
    with patch("cleanplex.web.routes.settings.plex_mod.PlexClient") as MockClient:
        instance = MockClient.return_value
        instance.test_connection = AsyncMock(return_value=(True, "My Plex"))
        resp = await http_client.post("/api/settings/test-connection")
    assert resp.json()["ok"] is True
    assert resp.json()["message"] == "My Plex"


# ── GET /api/settings/detector-labels ─────────────────────────────────────────

async def test_get_detector_labels_returns_list(http_client):
    resp = await http_client.get("/api/settings/detector-labels")
    assert resp.status_code == 200
    labels = resp.json()["labels"]
    assert isinstance(labels, list)
    assert "FEMALE_BREAST_EXPOSED" in labels


# ── POST /api/settings/validate-model-path ────────────────────────────────────

async def test_validate_model_path_320n_always_ok(http_client):
    resp = await http_client.post(
        "/api/settings/validate-model-path",
        json={"nudenet_model": "320n", "nudenet_model_path": ""},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ── GET /api/users ─────────────────────────────────────────────────────────────

async def test_get_users_returns_empty_when_no_plex_no_filters(http_client):
    with patch("cleanplex.web.routes.users.plex_mod.get_client", side_effect=RuntimeError):
        resp = await http_client.get("/api/users")
    assert resp.status_code == 200
    assert resp.json()["users"] == []


async def test_get_users_includes_db_filter_entries(http_client):
    await db.upsert_user_filter("dave", enabled=False)
    with patch("cleanplex.web.routes.users.plex_mod.get_client", side_effect=RuntimeError):
        resp = await http_client.get("/api/users")
    users = resp.json()["users"]
    dave = next((u for u in users if u["username"] == "dave"), None)
    assert dave is not None
    assert dave["enabled"] is False


async def test_get_users_merges_plex_users_with_filters(http_client):
    from cleanplex.plex_client import PlexUser
    await db.upsert_user_filter("alice", enabled=False)
    mock_client = make_mock_plex_client()
    mock_client.get_all_users = AsyncMock(return_value=[
        PlexUser(username="alice", thumb="/alice.jpg"),
        PlexUser(username="bob", thumb=""),
    ])
    with patch("cleanplex.web.routes.users.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.get("/api/users")
    users = {u["username"]: u for u in resp.json()["users"]}
    assert users["alice"]["enabled"] is False
    assert users["bob"]["enabled"] is True  # default when no filter record


# ── PUT /api/users/{username} ─────────────────────────────────────────────────

async def test_update_user_filter_enables(http_client):
    resp = await http_client.put("/api/users/eve", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    row = await db.get_user_filter("eve")
    assert row["enabled"] == 1


async def test_update_user_filter_disables(http_client):
    resp = await http_client.put("/api/users/frank", json={"enabled": False})
    assert resp.status_code == 200
    row = await db.get_user_filter("frank")
    assert row["enabled"] == 0


# ── GET /api/thumbnails/{segment_id} ──────────────────────────────────────────

async def test_get_thumbnail_returns_404_for_missing_segment(http_client):
    resp = await http_client.get("/api/thumbnails/99999")
    assert resp.status_code == 404


async def test_get_thumbnail_returns_404_when_no_path(http_client):
    seg_id = await db.insert_segment("g-thumb", "T", start_ms=0, end_ms=1000)
    resp = await http_client.get(f"/api/thumbnails/{seg_id}")
    assert resp.status_code == 404


async def test_get_thumbnail_serves_file(http_client):
    # Write a fake JPEG to a temp file
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        f.write(b"\xff\xd8\xff" + b"\x00" * 100)
        tmp_path = f.name
    try:
        seg_id = await db.insert_segment("g-real-thumb", "T", start_ms=0, end_ms=1000,
                                          thumbnail_path=tmp_path)
        resp = await http_client.get(f"/api/thumbnails/{seg_id}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
    finally:
        os.unlink(tmp_path)
