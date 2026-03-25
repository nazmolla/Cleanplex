"""Shared pytest fixtures for Cleanplex tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio

from cleanplex import database as db
from cleanplex.web.app import create_app


# ── Database fixtures ──────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def setup_db(tmp_path):
    """Initialise a fresh SQLite database in a temp directory for each test."""
    db.set_db_path(tmp_path / "cleanplex.db")
    await db.init_db()
    yield


# ── HTTP client for integration tests ────────────────────────────────────────

@pytest_asyncio.fixture
async def http_client(setup_db):
    """Async HTTPX client pointed at the FastAPI app under test."""
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


# ── Mock PlexClient factory ────────────────────────────────────────────────────

def make_mock_plex_client(
    *,
    sessions=None,
    library_sections=None,
    library_items=None,
    seek_result=True,
    show_art=("", "", ""),
    connection_ok=True,
):
    """Return a MagicMock with the same async interface as PlexClient."""
    mock = MagicMock()
    mock.get_active_sessions = AsyncMock(return_value=sessions or [])
    mock.get_library_sections = AsyncMock(return_value=library_sections or [])
    mock.get_library_items = AsyncMock(return_value=library_items or [])
    mock.seek = AsyncMock(return_value=seek_result)
    mock.get_episode_show_art = AsyncMock(return_value=show_art)
    mock.thumb_url = MagicMock(side_effect=lambda p: f"http://plex{p}" if p else "")
    mock.fetch_image = AsyncMock(return_value=(b"imgdata", "image/jpeg"))
    mock.update_cleanplex_summary = AsyncMock(return_value=True)
    mock.test_connection = AsyncMock(return_value=(connection_ok, "My Plex"))
    return mock
