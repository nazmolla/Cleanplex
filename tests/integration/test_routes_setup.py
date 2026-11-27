"""First-run setup routes (#245): status, detect, Sign in with Plex, finish."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from cleanplex import database as db
from cleanplex.web.routes import setup_routes

pytestmark = pytest.mark.usefixtures("setup_db")


def _fake_try(ok_urls: dict[str, str]):
    async def _try(url: str, token: str):
        return (True, ok_urls[url]) if url in ok_urls else (False, "unreachable")
    return _try


async def test_fresh_install_needs_setup(http_client):
    data = (await http_client.get("/api/setup/status")).json()
    assert data["done"] is False
    assert data["plex_connected"] is False


async def test_install_with_scans_counts_as_set_up(http_client):
    async with db.get_connection() as conn:
        await conn.execute("INSERT INTO scan_jobs(plex_guid, title, status) VALUES('g', 't', 'done')")
        await conn.commit()
    assert (await http_client.get("/api/setup/status")).json()["done"] is True


async def test_start_keeps_wizard_until_finish(http_client):
    await http_client.post("/api/setup/start")
    assert (await http_client.get("/api/setup/status")).json()["done"] is False
    await http_client.post("/api/setup/finish")
    assert (await http_client.get("/api/setup/status")).json()["done"] is True
    # Opening the wizard again later must not undo a finished setup.
    await http_client.post("/api/setup/start")
    assert (await http_client.get("/api/setup/status")).json()["done"] is True


async def test_detect_saves_local_plex(http_client):
    with patch.object(setup_routes, "_local_token", return_value="tok"), \
         patch.object(setup_routes, "_try", new=_fake_try({setup_routes.LOCAL_URL: "Home"})), \
         patch.object(setup_routes.plex_mod, "init_client") as init:
        data = (await http_client.post("/api/setup/detect")).json()
    assert data == {"ok": True, "server_name": "Home", "plex_url": setup_routes.LOCAL_URL}
    assert await db.get_setting("plex_token") == "tok"
    init.assert_called_once_with(setup_routes.LOCAL_URL, "tok")


async def test_detect_asks_to_sign_in_when_plex_answers_but_token_fails(http_client):
    with patch.object(setup_routes, "_local_token", return_value="stale"), \
         patch.object(setup_routes, "_try", new=_fake_try({})), \
         patch.object(setup_routes, "_plex_answers_here", new=AsyncMock(return_value=True)):
        data = (await http_client.post("/api/setup/detect")).json()
    assert data == {"ok": False, "reason": "sign_in_needed"}


async def test_detect_reports_missing_plex(http_client):
    with patch.object(setup_routes, "_local_token", return_value=""), \
         patch.object(setup_routes, "_plex_answers_here", new=AsyncMock(return_value=False)):
        data = (await http_client.post("/api/setup/detect")).json()
    assert data == {"ok": False, "reason": "not_found"}
    assert await db.get_setting("plex_token") == ""


class _FakeHttp:
    """Stands in for httpx.AsyncClient: answers plex.tv's pins and resources endpoints."""

    def __init__(self, pin: dict, resources: list):
        self.pin, self.resources = pin, resources

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _resp(self, url: str, body):
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    async def post(self, url, **kw):
        return self._resp(url, {"id": 7, "code": "abcd"})

    async def get(self, url, **kw):
        return self._resp(url, self.pin if "/pins/" in url else self.resources)


def _local_plex(machine_by_token: dict[str, str]):
    """A PlexClient stand-in: only tokens in the map reach a server on this machine."""
    def make(url: str, token: str):
        c = MagicMock()
        c.get_machine_identifier = AsyncMock(return_value=machine_by_token.get(token, ""))
        c.test_connection = AsyncMock(return_value=(token in machine_by_token, "Home"))
        c.close = AsyncMock()
        return c
    return make


RESOURCES = [
    {"provides": "player", "owned": True, "accessToken": "x"},
    {"provides": "server", "owned": False, "accessToken": "shared", "clientIdentifier": "s"},
    {"provides": "server", "owned": True, "accessToken": "far", "clientIdentifier": "nas"},
    {"provides": "server", "owned": True, "accessToken": "srv", "clientIdentifier": "mine"},
]


async def test_pin_flow_links_the_server_on_this_machine(http_client):
    fake = _FakeHttp({"authToken": "account"}, RESOURCES)
    with patch.object(setup_routes.httpx, "AsyncClient", return_value=fake), \
         patch.object(setup_routes.plex_mod, "PlexClient", side_effect=_local_plex({"srv": "mine"})), \
         patch.object(setup_routes.plex_mod, "init_client"):
        pin = (await http_client.post("/api/setup/plex/pin")).json()
        assert pin["id"] == 7 and "code=abcd" in pin["auth_url"]
        data = (await http_client.get("/api/setup/plex/pin/7")).json()
    assert data["status"] == "linked"
    assert data["plex_url"] == setup_routes.LOCAL_URL
    # The server's own token is stored, never the account token.
    assert await db.get_setting("plex_token") == "srv"


async def test_pin_flow_refuses_a_server_elsewhere(http_client):
    """Hayaa must sit beside Plex: an owned server on another machine is an error, not a link."""
    fake = _FakeHttp({"authToken": "account"}, RESOURCES)
    with patch.object(setup_routes.httpx, "AsyncClient", return_value=fake), \
         patch.object(setup_routes.plex_mod, "PlexClient", side_effect=_local_plex({})):
        data = (await http_client.get("/api/setup/plex/pin/7")).json()
    assert data == {"status": "error", "reason": "not_on_this_machine"}
    assert await db.get_setting("plex_token") == ""


async def test_pin_flow_rejects_a_different_local_server(http_client):
    """A token that works locally but belongs to another server (machine id differs) is refused."""
    fake = _FakeHttp({"authToken": "account"}, RESOURCES)
    with patch.object(setup_routes.httpx, "AsyncClient", return_value=fake), \
         patch.object(setup_routes.plex_mod, "PlexClient", side_effect=_local_plex({"far": "mine"})):
        data = (await http_client.get("/api/setup/plex/pin/7")).json()
    assert data["status"] == "error"


async def test_pin_pending_until_approved(http_client):
    fake = _FakeHttp({"authToken": None}, [])
    with patch.object(setup_routes.httpx, "AsyncClient", return_value=fake):
        data = (await http_client.get("/api/setup/plex/pin/7")).json()
    assert data == {"status": "pending"}
