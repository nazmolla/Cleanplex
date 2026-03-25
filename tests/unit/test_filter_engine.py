"""Unit tests for filter_engine.py — seek decision logic."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import cleanplex.filter_engine as fe
from cleanplex.plex_client import ActiveSession


def _session(
    *,
    session_key: str = "sess-1",
    plex_guid: str = "guid-1",
    rating_key: str = "rk-1",
    position_ms: int = 0,
    is_controllable: bool = True,
    user: str = "alice",
    client_identifier: str = "client-abc",
    client_address: str = "192.168.1.10",
    client_port: int = 32500,
) -> ActiveSession:
    return ActiveSession(
        session_key=session_key,
        user=user,
        title="Movie",
        full_title="Movie",
        plex_guid=plex_guid,
        rating_key=rating_key,
        media_type="movie",
        position_ms=position_ms,
        duration_ms=7200000,
        client_identifier=client_identifier,
        client_title="Plex Web",
        is_controllable=is_controllable,
        client_address=client_address,
        client_port=client_port,
    )


def _make_client(seek_result: bool = True) -> MagicMock:
    client = MagicMock()
    client.seek = AsyncMock(return_value=seek_result)
    return client


def _segs(start: int, end: int) -> list[dict]:
    return [{"start_ms": start, "end_ms": end, "confidence": 0.9, "plex_guid": "guid-1"}]


@pytest.fixture(autouse=True)
def reset_filter_state():
    """Clear global filter state before each test to prevent cross-test bleed."""
    fe._recently_skipped.clear()
    fe._seek_backoff_until.clear()
    yield
    fe._recently_skipped.clear()
    fe._seek_backoff_until.clear()


# ── Not controllable ───────────────────────────────────────────────────────────

async def test_non_controllable_session_skips_without_seek():
    session = _session(is_controllable=False)
    client = _make_client()
    with patch("cleanplex.filter_engine.db") as mock_db:
        mock_db.get_segments_for_guid = AsyncMock(return_value=[])
        await fe.process(session, client, skip_buffer_ms=3000)
    client.seek.assert_not_called()


# ── No segments ────────────────────────────────────────────────────────────────

async def test_no_segments_does_not_seek():
    session = _session(position_ms=5000)
    client = _make_client()
    with patch("cleanplex.filter_engine.db") as mock_db:
        mock_db.get_segments_for_guid = AsyncMock(return_value=[])
        mock_db.get_segments_by_rating_key = AsyncMock(return_value=[])
        await fe.process(session, client, skip_buffer_ms=3000)
    client.seek.assert_not_called()


# ── GUID mismatch fallback ─────────────────────────────────────────────────────

async def test_guid_mismatch_falls_back_to_rating_key():
    session = _session(position_ms=50000, rating_key="rk-fallback")
    client = _make_client()
    segments = _segs(45000, 60000)  # position is within segment

    with patch("cleanplex.filter_engine.db") as mock_db:
        mock_db.get_segments_for_guid = AsyncMock(return_value=[])
        mock_db.get_segments_by_rating_key = AsyncMock(return_value=segments)
        await fe.process(session, client, skip_buffer_ms=3000)

    mock_db.get_segments_by_rating_key.assert_awaited_once_with("rk-fallback")


# ── Lookahead trigger ──────────────────────────────────────────────────────────

async def test_position_within_lookahead_triggers_seek():
    # Segment starts at 30000ms (after 5s expansion → 25000ms), lookahead=5000ms
    # Position at 21000ms is within lookahead window of 25000ms
    session = _session(position_ms=21000)
    client = _make_client()
    # Raw segment: start=30000, end=40000 → expanded: start=25000, end=45000
    with patch("cleanplex.filter_engine.db") as mock_db:
        mock_db.get_segments_for_guid = AsyncMock(return_value=_segs(30000, 40000))
        await fe.process(session, client, skip_buffer_ms=3000, lookahead_ms=5000)

    client.seek.assert_awaited_once()
    _, seek_ms, *_ = client.seek.call_args[0]
    # Seek target is the expanded start (25000ms)
    assert seek_ms == 25000


async def test_position_before_lookahead_does_not_seek():
    # Position at 5000ms, lookahead window starts at 25000ms - 5000ms = 20000ms
    session = _session(position_ms=5000)
    client = _make_client()
    with patch("cleanplex.filter_engine.db") as mock_db:
        mock_db.get_segments_for_guid = AsyncMock(return_value=_segs(30000, 40000))
        await fe.process(session, client, skip_buffer_ms=3000, lookahead_ms=5000)

    client.seek.assert_not_called()


async def test_position_inside_segment_triggers_seek():
    # Position is within expanded segment bounds
    session = _session(position_ms=35000)
    client = _make_client()
    with patch("cleanplex.filter_engine.db") as mock_db:
        mock_db.get_segments_for_guid = AsyncMock(return_value=_segs(30000, 40000))
        await fe.process(session, client, skip_buffer_ms=3000, lookahead_ms=5000)

    client.seek.assert_awaited_once()


async def test_position_past_segment_does_not_seek():
    # Position is beyond the expanded segment end
    session = _session(position_ms=55000)
    client = _make_client()
    with patch("cleanplex.filter_engine.db") as mock_db:
        mock_db.get_segments_for_guid = AsyncMock(return_value=_segs(30000, 40000))
        await fe.process(session, client, skip_buffer_ms=3000, lookahead_ms=5000)

    client.seek.assert_not_called()


# ── Recently skipped guard ─────────────────────────────────────────────────────

async def test_recently_skipped_prevents_re_trigger():
    session = _session(position_ms=35000)
    client = _make_client()
    # Simulate a previous skip that set end to 50000ms
    fe._recently_skipped["sess-1"] = 50000

    with patch("cleanplex.filter_engine.db") as mock_db:
        mock_db.get_segments_for_guid = AsyncMock(return_value=_segs(30000, 40000))
        await fe.process(session, client, skip_buffer_ms=3000)

    client.seek.assert_not_called()


async def test_recently_skipped_cleared_when_past_end():
    # Position has moved past the skipped end → entry should be removed
    session = _session(position_ms=60000)
    client = _make_client()
    fe._recently_skipped["sess-1"] = 50000

    with patch("cleanplex.filter_engine.db") as mock_db:
        mock_db.get_segments_for_guid = AsyncMock(return_value=_segs(30000, 40000))
        await fe.process(session, client, skip_buffer_ms=3000)

    assert "sess-1" not in fe._recently_skipped


# ── Backoff guard ──────────────────────────────────────────────────────────────

async def test_seek_backoff_prevents_retry():
    session = _session(position_ms=35000)
    client = _make_client()
    # Set a backoff in the future
    fe._seek_backoff_until["sess-1"] = time.time() + 60

    with patch("cleanplex.filter_engine.db") as mock_db:
        mock_db.get_segments_for_guid = AsyncMock(return_value=_segs(30000, 40000))
        await fe.process(session, client, skip_buffer_ms=3000)

    client.seek.assert_not_called()


async def test_failed_seek_sets_backoff():
    session = _session(position_ms=35000)
    client = _make_client(seek_result=False)

    with patch("cleanplex.filter_engine.db") as mock_db:
        mock_db.get_segments_for_guid = AsyncMock(return_value=_segs(30000, 40000))
        await fe.process(session, client, skip_buffer_ms=3000)

    assert "sess-1" in fe._seek_backoff_until
    assert fe._seek_backoff_until["sess-1"] > time.time()


async def test_successful_seek_records_recently_skipped():
    session = _session(position_ms=35000)
    client = _make_client(seek_result=True)

    with patch("cleanplex.filter_engine.db") as mock_db:
        mock_db.get_segments_for_guid = AsyncMock(return_value=_segs(30000, 40000))
        await fe.process(session, client, skip_buffer_ms=3000)

    # Should record end_ms of the expanded segment (45000)
    assert "sess-1" in fe._recently_skipped
    assert fe._recently_skipped["sess-1"] == 45000


async def test_successful_seek_clears_backoff():
    session = _session(position_ms=35000)
    client = _make_client(seek_result=True)
    fe._seek_backoff_until["sess-1"] = time.time() - 1  # expired backoff

    with patch("cleanplex.filter_engine.db") as mock_db:
        mock_db.get_segments_for_guid = AsyncMock(return_value=_segs(30000, 40000))
        await fe.process(session, client, skip_buffer_ms=3000)

    assert "sess-1" not in fe._seek_backoff_until
