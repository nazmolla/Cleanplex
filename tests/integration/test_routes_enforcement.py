"""Enforcement routes (#184)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cleanplex import database as db
from cleanplex.plex_client import PlexClient

pytestmark = pytest.mark.usefixtures("setup_db")


def _client(prefs=None):
    c = MagicMock()
    c.ENFORCEMENT_PREFS = PlexClient.ENFORCEMENT_PREFS
    c.get_prefs = AsyncMock(return_value=prefs or {"RelayEnabled": "1", "DlnaEnabled": "1", "TranscodeCountLimit": "2"})
    c.set_prefs = AsyncMock()
    return c


async def test_defaults_are_off_with_relay_and_dlna_recommended_off(http_client):
    with patch("cleanplex.web.routes.enforcement_routes.plex_mod.get_client", return_value=_client()):
        data = (await http_client.get("/api/enforcement")).json()
    assert data["enabled"] is False and data["mute_mode"] == "leave" and data["backstop"] is True
    assert data["plex"] == {"relay": "1", "dlna": "1", "transcode_limit": "2"}
    assert data["recommendations"] == {"relay": False, "dlna": False}


async def test_enabling_and_mute_mode_are_stored(http_client):
    with patch("cleanplex.web.routes.enforcement_routes.plex_mod.get_client", side_effect=RuntimeError):
        resp = await http_client.post("/api/enforcement", json={"enabled": True, "mute_mode": "cut"})
    assert resp.status_code == 200 and resp.json()["enabled"] is True
    assert await db.get_setting("enforcement_mute_mode") == "cut"


async def test_an_unknown_mute_mode_is_rejected(http_client):
    resp = await http_client.post("/api/enforcement", json={"mute_mode": "blank"})
    assert resp.status_code == 422


async def test_plex_settings_are_applied_with_plex_keys(http_client):
    client = _client()
    with patch("cleanplex.web.routes.enforcement_routes.plex_mod.get_client", return_value=client):
        resp = await http_client.post("/api/enforcement/plex-settings", json={"relay": False, "dlna": False, "transcode_limit": 6})
    assert resp.status_code == 200
    client.set_prefs.assert_awaited_once_with({"RelayEnabled": "0", "DlnaEnabled": "0", "TranscodeCountLimit": "6"})


async def test_plex_settings_need_plex_and_something_to_change(http_client):
    with patch("cleanplex.web.routes.enforcement_routes.plex_mod.get_client", side_effect=RuntimeError):
        assert (await http_client.post("/api/enforcement/plex-settings", json={"relay": False})).status_code == 503
    with patch("cleanplex.web.routes.enforcement_routes.plex_mod.get_client", return_value=_client()):
        assert (await http_client.post("/api/enforcement/plex-settings", json={})).status_code == 422
