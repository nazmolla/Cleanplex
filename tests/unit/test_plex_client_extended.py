"""Extended unit tests for plex_client.py — mocking asyncio.to_thread paths."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from cleanplex.plex_client import PlexClient, ActiveSession, LibrarySection, MediaItem, PlexUser


def _make_client(seek_transport=None) -> PlexClient:
    c = PlexClient("http://plex:32400", "token")
    if seek_transport:
        c._http = httpx.AsyncClient(transport=seek_transport)
    return c


def _mock_server():
    srv = MagicMock()
    srv.friendlyName = "My Plex"
    return srv


# ── test_connection ────────────────────────────────────────────────────────────

async def test_test_connection_success():
    c = _make_client()
    srv = _mock_server()

    with patch("cleanplex.plex_client.asyncio.to_thread", new=AsyncMock(return_value=srv)):
        ok, name = await c.test_connection()

    assert ok is True
    assert name == "My Plex"


async def test_test_connection_failure():
    c = _make_client()
    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=Exception("connection refused")):
        ok, msg = await c.test_connection()
    assert ok is False
    assert "connection refused" in msg


# ── get_active_sessions ────────────────────────────────────────────────────────

async def test_get_active_sessions_empty():
    c = _make_client()
    srv = _mock_server()
    srv.sessions = MagicMock(return_value=[])

    call_no = 0

    async def fake_to_thread(func, *args, **kwargs):
        nonlocal call_no
        call_no += 1
        if call_no == 1:
            return srv  # _get_server
        return srv.sessions()  # sessions()

    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=fake_to_thread):
        sessions = await c.get_active_sessions()

    assert sessions == []


async def test_get_active_sessions_returns_empty_on_exception():
    c = _make_client()
    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=Exception("network error")):
        sessions = await c.get_active_sessions()
    assert sessions == []


# ── seek ──────────────────────────────────────────────────────────────────────

async def test_seek_success_via_server_proxy():
    c = _make_client()
    srv = _mock_server()
    srv.query = MagicMock(return_value=None)

    call_no = 0

    async def fake_to_thread(func, *args, **kwargs):
        nonlocal call_no
        call_no += 1
        if call_no == 1:
            return srv
        return None  # srv.query

    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=fake_to_thread):
        result = await c.seek("client-id", 30000)

    assert result is True


async def test_seek_falls_back_to_direct_http_on_proxy_failure():
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b"ok"))
    c = _make_client(seek_transport=transport)

    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=Exception("proxy failed")):
        result = await c.seek("client-id", 30000, client_address="192.168.1.10", client_port=32500)

    assert result is True


async def test_seek_returns_false_when_no_client_address_and_proxy_fails():
    c = _make_client()
    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=Exception("proxy failed")):
        result = await c.seek("client-id", 30000, client_address="")
    assert result is False


async def test_seek_returns_false_when_all_variants_fail():
    transport = httpx.MockTransport(lambda req: httpx.Response(400, content=b"bad"))
    c = _make_client(seek_transport=transport)
    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=Exception("proxy failed")):
        result = await c.seek("client-id", 30000, client_address="192.168.1.10")
    assert result is False


# ── get_library_sections ──────────────────────────────────────────────────────

async def test_get_library_sections_returns_list():
    c = _make_client()
    srv = _mock_server()

    mock_section = MagicMock()
    mock_section.key = "1"
    mock_section.title = "Movies"
    mock_section.type = "movie"

    call_no = 0

    async def fake_to_thread(func, *args, **kwargs):
        nonlocal call_no
        call_no += 1
        if call_no == 1:
            return srv
        return [mock_section]  # sections()

    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=fake_to_thread):
        sections = await c.get_library_sections()

    assert len(sections) == 1
    assert isinstance(sections[0], LibrarySection)
    assert sections[0].section_id == "1"
    assert sections[0].section_type == "movie"


async def test_get_library_sections_excludes_non_video_sections():
    c = _make_client()
    srv = _mock_server()

    music = MagicMock()
    music.key = "5"
    music.title = "Music"
    music.type = "artist"  # Not movie or show → excluded

    call_no = 0

    async def fake_to_thread(func, *args, **kwargs):
        nonlocal call_no
        call_no += 1
        if call_no == 1:
            return srv
        return [music]

    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=fake_to_thread):
        sections = await c.get_library_sections()

    assert sections == []


async def test_get_library_sections_returns_empty_on_exception():
    c = _make_client()
    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=Exception("plex down")):
        sections = await c.get_library_sections()
    assert sections == []


# ── _media_item_from_plex ─────────────────────────────────────────────────────

def test_media_item_from_plex_movie():
    c = _make_client()

    mock_item = MagicMock()
    mock_item.type = "movie"
    mock_item.ratingKey = 42
    mock_item.title = "Inception"
    mock_item.year = 2010
    mock_item.thumb = "/thumb/42"
    mock_item.contentRating = "PG-13"
    mock_item.media = [MagicMock()]
    mock_item.media[0].parts = [MagicMock()]
    mock_item.media[0].parts[0].file = "/media/inception.mkv"
    mock_item.guids = [MagicMock()]
    mock_item.guids[0].id = "imdb://tt1375666"
    del mock_item.grandparentTitle

    item = c._media_item_from_plex(mock_item, "lib1", "Movies")
    assert isinstance(item, MediaItem)
    assert item.title == "Inception"
    assert item.file_path == "/media/inception.mkv"
    assert item.rating_key == "42"


def test_media_item_from_plex_episode_includes_show_in_title():
    c = _make_client()

    mock_ep = MagicMock()
    mock_ep.type = "episode"
    mock_ep.ratingKey = 100
    mock_ep.title = "Pilot"
    mock_ep.year = 2020
    mock_ep.thumb = ""
    mock_ep.contentRating = "TV-MA"
    mock_ep.grandparentTitle = "Breaking Bad"
    mock_ep.parentTitle = "Season 1"
    mock_ep.media = [MagicMock()]
    mock_ep.media[0].parts = [MagicMock()]
    mock_ep.media[0].parts[0].file = "/ep.mkv"
    mock_ep.guids = [MagicMock()]
    mock_ep.guids[0].id = "imdb://ep1"

    item = c._media_item_from_plex(mock_ep, "lib2", "Shows")
    assert "Breaking Bad" in item.title
    assert "Pilot" in item.title


def test_media_item_from_plex_returns_none_on_error():
    c = _make_client()
    result = c._media_item_from_plex(None, "lib", "L")
    assert result is None


# ── close ─────────────────────────────────────────────────────────────────────

async def test_close_calls_aclose():
    c = _make_client()
    mock_http = AsyncMock()
    c._http = mock_http
    await c.close()
    mock_http.aclose.assert_awaited_once()


# ── invalidate ────────────────────────────────────────────────────────────────

def test_invalidate_clears_server():
    c = _make_client()
    c._server = MagicMock()
    c.invalidate()
    assert c._server is None


# ── update_cleanplex_summary ──────────────────────────────────────────────────

async def test_update_cleanplex_summary_success():
    c = _make_client()
    srv = _mock_server()

    mock_item = MagicMock()
    mock_item.summary = "Original summary."
    mock_item.editSummary = MagicMock()

    call_no = 0

    async def fake_to_thread(func, *args, **kwargs):
        nonlocal call_no
        call_no += 1
        if call_no == 1:
            return srv
        if "fetchItem" in str(func) or (args and isinstance(args[0], int)):
            return mock_item
        return None

    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=fake_to_thread):
        result = await c.update_cleanplex_summary("42", "Scanned", 3)

    assert result is True


async def test_update_cleanplex_summary_returns_false_on_exception():
    c = _make_client()
    with patch("cleanplex.plex_client.asyncio.to_thread", side_effect=Exception("fetch failed")):
        result = await c.update_cleanplex_summary("42", "Scanned", 3)
    assert result is False
