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


# ── SigLIP fallback settings (issue #83) ──────────────────────────────────────

async def test_siglip_settings_have_defaults_and_persist(http_client):
    before = (await http_client.get("/api/settings")).json()
    assert (before["siglip_enabled"], before["siglip_threshold"], before["siglip_min_hits"]) == ("0", "0.3", "2")

    resp = await http_client.put("/api/settings", json={"siglip_enabled": "1", "siglip_threshold": "0.35", "siglip_min_hits": "3"})
    after = (await http_client.get("/api/settings")).json()

    assert resp.status_code == 200
    assert (after["siglip_enabled"], after["siglip_threshold"], after["siglip_min_hits"]) == ("1", "0.35", "3")


@pytest.mark.parametrize("body", [
    {"siglip_enabled": "yes"},
    {"siglip_threshold": "1.5"},
    {"siglip_threshold": "abc"},
    {"siglip_min_hits": "0"},
])
async def test_invalid_siglip_settings_are_rejected(http_client, body):
    assert (await http_client.put("/api/settings", json=body)).status_code == 422


async def test_hw_decode_defaults_on_and_can_be_turned_off(http_client):
    assert (await http_client.get("/api/settings")).json()["hw_decode"] == "1"

    assert (await http_client.put("/api/settings", json={"hw_decode": "0"})).status_code == 200
    assert (await http_client.get("/api/settings")).json()["hw_decode"] == "0"
    assert (await http_client.put("/api/settings", json={"hw_decode": "yes"})).status_code == 422


async def test_hover_preview_settings_default_to_the_evening_and_validate(http_client):
    s = (await http_client.get("/api/settings")).json()
    assert (s["hover_previews_enabled"], s["hover_window_start"], s["hover_window_end"]) == ("1", "00:00", "07:00")

    assert (await http_client.put("/api/settings", json={"hover_window_start": "19:30"})).status_code == 200
    assert (await http_client.get("/api/settings")).json()["hover_window_start"] == "19:30"
    assert (await http_client.put("/api/settings", json={"hover_window_end": "25:00"})).status_code == 422
    assert (await http_client.put("/api/settings", json={"hover_previews_enabled": "on"})).status_code == 422


# ── per-label thresholds, credits, inference threads (#120, #121, #123) ──────────

async def _put(http_client, body):
    with patch("cleanplex.web.routes.settings.scan_mod.request_scanner_restart", new=AsyncMock()) as restart:
        resp = await http_client.put("/api/settings", json=body)
    return resp, restart


async def test_put_accepts_label_threshold_maps(http_client):
    resp, _ = await _put(http_client, {
        "label_thresholds": '{"MALE_GENITALIA_EXPOSED": 0.75}',
        "siglip_label_thresholds": '{"BUTTOCKS_EXPOSED": 0.9}',
    })
    assert resp.status_code == 200
    assert await db.get_setting("label_thresholds") == '{"MALE_GENITALIA_EXPOSED": 0.75}'


@pytest.mark.parametrize("bad", [
    "not json", "[0.5]", '{"NOT_A_LABEL": 0.5}', '{"BUTTOCKS_EXPOSED": 1.5}',
    '{"BUTTOCKS_EXPOSED": "0.5"}', '{"BUTTOCKS_EXPOSED": true}',
])
async def test_put_rejects_invalid_label_threshold_maps(http_client, bad):
    resp, _ = await _put(http_client, {"label_thresholds": bad})
    assert resp.status_code == 422


async def test_put_rejects_out_of_range_inference_threads(http_client):
    resp, _ = await _put(http_client, {"inference_threads": "99"})
    assert resp.status_code == 422


async def test_changing_inference_threads_restarts_the_scanner_pool(http_client):
    resp, restart = await _put(http_client, {"inference_threads": "2"})
    assert resp.status_code == 200
    restart.assert_awaited_once()


async def test_put_ignore_credits_toggle(http_client):
    resp, _ = await _put(http_client, {"ignore_credits": "0"})
    assert resp.status_code == 200
    assert await db.get_setting("ignore_credits") == "0"


# ── refine step (#133) ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["0", "100", "500", "5000"])
async def test_put_accepts_a_refine_step(http_client, value):
    resp, _ = await _put(http_client, {"refine_step_ms": value})
    assert resp.status_code == 200
    assert await db.get_setting("refine_step_ms") == value


@pytest.mark.parametrize("value", ["-1", "99", "5001", "fast", "50.5", ""])
async def test_put_rejects_an_invalid_refine_step(http_client, value):
    resp, _ = await _put(http_client, {"refine_step_ms": value})
    assert resp.status_code == 422


async def test_hover_when_idle_is_on_by_default_and_can_be_turned_off(http_client):
    assert (await http_client.get("/api/settings")).json()["hover_when_idle"] == "1"
    resp, _ = await _put(http_client, {"hover_when_idle": "0"})
    assert resp.status_code == 200 and await db.get_setting("hover_when_idle") == "0"
    resp, _ = await _put(http_client, {"hover_when_idle": "maybe"})
    assert resp.status_code == 422
