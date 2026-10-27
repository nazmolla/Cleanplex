"""Unit tests for filter_engine.py — seek/mute decision logic."""

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
    volume: int | None = None,
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
        volume=volume,
    )


def _make_client(seek_result: bool = True, volume_result: bool = True) -> MagicMock:
    client = MagicMock()
    client.seek = AsyncMock(return_value=seek_result)
    client.set_volume = AsyncMock(return_value=volume_result)
    client._forget_profile = AsyncMock()
    return client


def _segs(start: int, end: int, **overrides) -> list[dict]:
    seg = {
        "start_ms": start,
        "end_ms": end,
        "confidence": 0.9,
        "plex_guid": "guid-1",
        "category": "nudity",
        "severity": "high",
        "action": "skip",
    }
    seg.update(overrides)
    return [seg]


def _mock_db(mock_db, segments, prefs=None):
    mock_db.get_segments_for_guid = AsyncMock(return_value=segments)
    mock_db.get_segments_by_rating_key = AsyncMock(return_value=[])
    mock_db.get_user_category_prefs = AsyncMock(return_value=prefs or {})
    mock_db.record_skip_event = AsyncMock(return_value=1)
    return mock_db


@pytest.fixture(autouse=True)
def reset_filter_state():
    """Clear global filter state before each test to prevent cross-test bleed."""
    states = (fe._recently_skipped, fe._seek_backoff_until, fe._muted_sessions, fe._pending_verification)
    for state in states:
        state.clear()
    yield
    for state in states:
        state.clear()


# ── Not controllable ───────────────────────────────────────────────────────────

async def test_non_controllable_session_skips_without_seek():
    session = _session(is_controllable=False)
    client = _make_client()
    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, [])
        await fe.process(session, client)
    client.seek.assert_not_called()


# ── No segments ────────────────────────────────────────────────────────────────

async def test_no_segments_does_not_seek():
    session = _session(position_ms=5000)
    client = _make_client()
    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, [])
        await fe.process(session, client)
    client.seek.assert_not_called()


# ── GUID mismatch fallback ─────────────────────────────────────────────────────

async def test_guid_mismatch_falls_back_to_rating_key():
    session = _session(position_ms=50000, rating_key="rk-fallback")
    client = _make_client()

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, [])
        mock_db.get_segments_by_rating_key = AsyncMock(return_value=_segs(45000, 60000))
        await fe.process(session, client)

    mock_db.get_segments_by_rating_key.assert_awaited_once_with("rk-fallback")


# ── Buffers (issue #58) ────────────────────────────────────────────────────────

async def test_pre_buffer_widens_segment_start():
    """A 3000ms pre-buffer must move the trigger and target to 27000, not 25000.

    Regression test for the hardcoded 5000ms expansion that ignored the setting.
    """
    session = _session(position_ms=27000)
    client = _make_client()
    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000))
        await fe.process(session, client, pre_buffer_ms=3000, post_buffer_ms=3000, lookahead_ms=0)

    client.seek.assert_awaited_once()
    _, seek_ms, *_ = client.seek.call_args[0]
    assert seek_ms == 27000


async def test_pre_buffer_is_honoured_when_changed():
    session = _session(position_ms=22000)
    client = _make_client()
    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000))
        await fe.process(session, client, pre_buffer_ms=8000, post_buffer_ms=1000, lookahead_ms=0)

    _, seek_ms, *_ = client.seek.call_args[0]
    assert seek_ms == 22000


async def test_post_buffer_bounds_the_trigger_window():
    """With a 1000ms post-buffer, position 42000 is past the widened end."""
    session = _session(position_ms=42000)
    client = _make_client()
    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000))
        await fe.process(session, client, pre_buffer_ms=3000, post_buffer_ms=1000, lookahead_ms=0)

    client.seek.assert_not_called()


async def test_pre_buffer_clamps_at_zero():
    session = _session(position_ms=500)
    client = _make_client()
    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(1000, 5000))
        await fe.process(session, client, pre_buffer_ms=8000, post_buffer_ms=3000, lookahead_ms=0)

    _, seek_ms, *_ = client.seek.call_args[0]
    assert seek_ms == 0


# ── Lookahead trigger ──────────────────────────────────────────────────────────

async def test_position_within_lookahead_triggers_seek():
    # Segment 30000-40000 with a 3000ms pre-buffer starts at 27000; a 5000ms
    # lookahead means position 23000 is inside the trigger window.
    session = _session(position_ms=23000)
    client = _make_client()
    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000))
        await fe.process(session, client, pre_buffer_ms=3000, post_buffer_ms=3000, lookahead_ms=5000)

    client.seek.assert_awaited_once()
    _, seek_ms, *_ = client.seek.call_args[0]
    assert seek_ms == 27000


async def test_position_before_lookahead_does_not_seek():
    session = _session(position_ms=5000)
    client = _make_client()
    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000))
        await fe.process(session, client, pre_buffer_ms=3000, post_buffer_ms=3000, lookahead_ms=5000)

    client.seek.assert_not_called()


async def test_position_inside_segment_triggers_seek():
    session = _session(position_ms=35000)
    client = _make_client()
    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000))
        await fe.process(session, client)

    client.seek.assert_awaited_once()


async def test_position_past_segment_does_not_seek():
    session = _session(position_ms=55000)
    client = _make_client()
    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000))
        await fe.process(session, client)

    client.seek.assert_not_called()


# ── Recently skipped guard ─────────────────────────────────────────────────────

async def test_recently_skipped_prevents_re_trigger():
    session = _session(position_ms=35000)
    client = _make_client()
    fe._recently_skipped["sess-1"] = 50000

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000))
        await fe.process(session, client)

    client.seek.assert_not_called()


async def test_recently_skipped_cleared_when_past_end():
    session = _session(position_ms=60000)
    client = _make_client()
    fe._recently_skipped["sess-1"] = 50000

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000))
        await fe.process(session, client)

    assert "sess-1" not in fe._recently_skipped


# ── Backoff guard ──────────────────────────────────────────────────────────────

async def test_seek_backoff_prevents_retry():
    session = _session(position_ms=35000)
    client = _make_client()
    fe._seek_backoff_until["sess-1"] = time.time() + 60

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000))
        await fe.process(session, client)

    client.seek.assert_not_called()


async def test_failed_seek_sets_backoff():
    session = _session(position_ms=35000)
    client = _make_client(seek_result=False)

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000))
        await fe.process(session, client)

    assert "sess-1" in fe._seek_backoff_until
    assert fe._seek_backoff_until["sess-1"] > time.time()


async def test_successful_seek_records_recently_skipped():
    session = _session(position_ms=35000)
    client = _make_client(seek_result=True)

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000))
        await fe.process(session, client, pre_buffer_ms=3000, post_buffer_ms=3000)

    assert fe._recently_skipped["sess-1"] == 43000


async def test_successful_seek_clears_backoff():
    session = _session(position_ms=35000)
    client = _make_client(seek_result=True)
    fe._seek_backoff_until["sess-1"] = time.time() - 1

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000))
        await fe.process(session, client)

    assert "sess-1" not in fe._seek_backoff_until


# ── Reaping stale session state (issue #59) ────────────────────────────────────

async def test_reap_drops_state_for_ended_sessions():
    fe._recently_skipped["gone"] = 1000
    fe._seek_backoff_until["gone"] = time.time() + 60
    fe._recently_skipped["alive"] = 2000

    await fe.reap({"alive"})

    assert "gone" not in fe._recently_skipped
    assert "gone" not in fe._seek_backoff_until
    assert fe._recently_skipped["alive"] == 2000


async def test_reap_lets_reused_session_key_skip_again():
    """Plex reuses sessionKey, so a stale entry must not suppress new playback."""
    session = _session(session_key="sess-1", position_ms=35000)
    client = _make_client()
    fe._recently_skipped["sess-1"] = 50000

    await fe.reap(set())  # previous session ended

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000))
        await fe.process(session, client)

    client.seek.assert_awaited_once()


async def test_reap_restores_volume_for_session_that_ended_muted():
    client = _make_client()
    fe._muted_sessions["sess-1"] = {
        "end_ms": 50000,
        "restore_to": 80,
        "client_identifier": "client-abc",
        "client_address": "192.168.1.10",
        "client_port": 32500,
    }

    await fe.reap(set(), client)

    client.set_volume.assert_awaited_once_with("client-abc", 80, "192.168.1.10", 32500)
    assert "sess-1" not in fe._muted_sessions


# ── Mute action (issue #63) ────────────────────────────────────────────────────

async def test_mute_action_sets_volume_and_does_not_seek():
    session = _session(position_ms=35000, volume=70)
    client = _make_client()

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000, action="mute", category="language"))
        await fe.process(session, client, pre_buffer_ms=0, post_buffer_ms=0)

    client.seek.assert_not_called()
    client.set_volume.assert_awaited_once_with("client-abc", 0, "192.168.1.10", 32500)
    assert fe._muted_sessions["sess-1"]["restore_to"] == 70


async def test_mute_restores_volume_once_past_segment():
    session = _session(position_ms=45000, volume=70)
    client = _make_client()
    fe._muted_sessions["sess-1"] = {
        "end_ms": 40000,
        "restore_to": 70,
        "client_identifier": "client-abc",
        "client_address": "192.168.1.10",
        "client_port": 32500,
    }

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, [])
        await fe.process(session, client)

    client.set_volume.assert_awaited_once_with("client-abc", 70, "192.168.1.10", 32500)
    assert "sess-1" not in fe._muted_sessions


async def test_unsupported_action_is_ignored_not_skipped():
    session = _session(position_ms=35000)
    client = _make_client()

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000, action="blank"))
        await fe.process(session, client)

    client.seek.assert_not_called()
    client.set_volume.assert_not_called()


# ── Per-user category preferences (issue #62) ──────────────────────────────────

async def test_no_prefs_filters_everything():
    session = _session(position_ms=35000)
    client = _make_client()

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000), prefs={})
        await fe.process(session, client)

    client.seek.assert_awaited_once()


async def test_high_level_skips_low_severity_segment():
    session = _session(position_ms=35000)
    client = _make_client()
    prefs = {"nudity": {"level": 3, "action": ""}}

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000, severity="low"), prefs=prefs)
        await fe.process(session, client)

    client.seek.assert_awaited_once()


async def test_zero_level_ignores_high_severity_segment():
    session = _session(position_ms=35000)
    client = _make_client()
    prefs = {"nudity": {"level": 3, "action": ""}, "violence": {"level": 0, "action": ""}}

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000, category="violence"), prefs=prefs)
        await fe.process(session, client)

    client.seek.assert_not_called()


async def test_category_absent_from_prefs_filters_at_full_strictness():
    """Regression for #77: setting one category must not switch the others off."""
    session = _session(position_ms=35000)
    client = _make_client()
    prefs = {"language": {"level": 1, "action": ""}}

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000, category="drugs", severity="low"), prefs=prefs)
        await fe.process(session, client)

    client.seek.assert_awaited_once()


async def test_user_action_override_turns_skip_into_mute():
    session = _session(position_ms=35000, volume=60)
    client = _make_client()
    prefs = {"language": {"level": 3, "action": "mute"}}

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000, category="language"), prefs=prefs)
        await fe.process(session, client)

    client.seek.assert_not_called()
    client.set_volume.assert_awaited()


# ── Skip event recording (issue #69) ───────────────────────────────────────────

async def test_successful_skip_is_recorded_with_category_and_latency():
    session = _session(position_ms=35000)
    client = _make_client()

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000))
        await fe.process(session, client)

    mock_db.record_skip_event.assert_awaited_once()
    kwargs = mock_db.record_skip_event.call_args.kwargs
    assert kwargs["category"] == "nudity"
    assert kwargs["success"] is True
    assert kwargs["action"] == "skip"
    assert kwargs["latency_ms"] >= 0


async def test_failed_skip_is_recorded_as_a_failure():
    session = _session(position_ms=35000)
    client = _make_client(seek_result=False)

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000))
        await fe.process(session, client)

    assert mock_db.record_skip_event.call_args.kwargs["success"] is False


async def test_mute_is_recorded_as_a_mute_event():
    session = _session(position_ms=35000, volume=70)
    client = _make_client()

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000, action="mute", category="language"))
        await fe.process(session, client)

    assert mock_db.record_skip_event.call_args.kwargs["action"] == "mute"


# ── Seek verification (issue #72) ──────────────────────────────────────────────

async def test_seek_that_did_not_move_is_recorded_as_a_failure():
    """A 2xx response is not proof: verify the position actually advanced."""
    client = _make_client()
    fe._pending_verification["sess-1"] = {
        "target_ms": 30000, "category": "nudity", "segment_id": 7, "latency_ms": 12,
    }
    # Next tick, the client is still sitting where it was.
    session = _session(position_ms=10000)

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, [])
        await fe.process(session, client)

    kwargs = mock_db.record_skip_event.call_args.kwargs
    assert kwargs["success"] is False
    assert kwargs["target_ms"] == 30000


async def test_failed_verification_invalidates_the_client_profile():
    client = _make_client()
    fe._pending_verification["sess-1"] = {
        "target_ms": 30000, "category": "nudity", "segment_id": None, "latency_ms": 5,
    }
    session = _session(position_ms=10000)

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, [])
        await fe.process(session, client)

    client._forget_profile.assert_awaited_once_with("client-abc")
    assert "sess-1" not in fe._recently_skipped


async def test_verified_seek_records_nothing_extra():
    client = _make_client()
    fe._pending_verification["sess-1"] = {
        "target_ms": 30000, "category": "nudity", "segment_id": None, "latency_ms": 5,
    }
    session = _session(position_ms=31000)

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, [])
        await fe.process(session, client)

    mock_db.record_skip_event.assert_not_awaited()
    client._forget_profile.assert_not_awaited()


async def test_verification_tolerates_keyframe_snapping():
    """Landing slightly short of the target is normal, not a failure."""
    client = _make_client()
    fe._pending_verification["sess-1"] = {
        "target_ms": 30000, "category": "nudity", "segment_id": None, "latency_ms": 5,
    }
    session = _session(position_ms=29000)  # 1s short, inside tolerance

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, [])
        await fe.process(session, client)

    mock_db.record_skip_event.assert_not_awaited()


async def test_verification_state_is_reaped_with_the_session():
    fe._pending_verification["gone"] = {"target_ms": 1, "category": "", "latency_ms": 0}

    await fe.reap(set())

    assert fe._pending_verification == {}


# ── Language-aware matching (issue #73) ────────────────────────────────────────

async def test_segment_without_language_applies_to_every_track():
    session = _session(position_ms=35000)
    session.audio_language = "fra"
    client = _make_client()

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000, language=""))
        await fe.process(session, client)

    client.seek.assert_awaited_once()


async def test_english_segment_does_not_fire_on_the_french_track():
    session = _session(position_ms=35000)
    session.audio_language = "fra"
    client = _make_client()

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000, language="eng", category="language", action="mute"))
        await fe.process(session, client)

    client.seek.assert_not_called()
    client.set_volume.assert_not_called()


async def test_matching_language_fires():
    session = _session(position_ms=35000, volume=50)
    session.audio_language = "eng"
    client = _make_client()

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000, language="eng", action="mute"))
        await fe.process(session, client)

    client.set_volume.assert_awaited()


async def test_two_and_three_letter_codes_are_treated_as_equal():
    """Plex reports 'en' or 'eng' depending on the source; both must match."""
    session = _session(position_ms=35000)
    session.audio_language = "en"
    client = _make_client()

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000, language="eng"))
        await fe.process(session, client)

    client.seek.assert_awaited_once()


async def test_unknown_session_language_applies_all_segments():
    session = _session(position_ms=35000)
    session.audio_language = ""
    client = _make_client()

    with patch("cleanplex.filter_engine.db") as mock_db:
        _mock_db(mock_db, _segs(30000, 40000, language="eng"))
        await fe.process(session, client)

    client.seek.assert_awaited_once()
