"""Enforcement pieces around the stream proxy: session store, live-skip stand-down, Stop backstop (#184)."""

from unittest.mock import AsyncMock

import httpx

from cleanplex import database as db
from cleanplex import filter_engine as fe
from cleanplex import watcher
from cleanplex.plex_client import ActiveSession, PlexClient


def _session(**kw) -> ActiveSession:
    base = dict(session_key="1", user="kid", title="Film", full_title="Film", plex_guid="g1", rating_key="71455",
                media_type="movie", position_ms=331_000, duration_ms=1, client_identifier="atv",
                client_title="Apple TV", is_controllable=True, playback_session_id="ps1")
    base.update(kw)
    return ActiveSession(**base)


async def _flagged_title():
    await db.upsert_scan_job("g1", "Film", "/m/f.mkv", "71455", "1", "Movies")
    await db.insert_segment("g1", "Film", 334_000, 350_000, 0.9, "", "FEMALE_BREAST_EXPOSED", severity="high")


async def test_proxy_session_round_trip_and_recency(setup_db):
    await db.save_proxy_session("s1", {"rating_key": "71455", "username": "kid", "client_id": "atv",
                                       "ranges": [(1, 2)], "spans": [(0, 10)], "removed": [3]})
    got = await db.get_proxy_session("s1")
    assert got["spans"] == [(0, 10)] and got["removed"] == [3] and got["username"] == "kid"
    assert await db.is_proxied("atv", "71455") is True
    assert await db.is_proxied("atv", "999") is False
    assert await db.get_proxy_session("missing") is None


async def test_live_skip_stands_down_for_a_stream_the_proxy_edits(setup_db):
    await _flagged_title()
    await db.save_proxy_session("s1", {"rating_key": "71455", "client_id": "atv"})
    client = AsyncMock()
    await fe.process(_session(), client, lookahead_ms=0)
    client.seek.assert_not_called()


async def test_backstop_does_nothing_before_the_front_door_is_installed(setup_db, monkeypatch):
    await _flagged_title()
    monkeypatch.setattr(watcher, "BACKSTOP_GRACE_S", 0)
    watcher._backstopped.clear(); watcher._first_seen.clear()
    client = AsyncMock()
    await watcher._backstop(_session(), client)
    client.terminate_session.assert_not_called()


async def test_backstop_stops_a_flagged_stream_that_never_passed_the_proxy_once(setup_db, monkeypatch):
    await _flagged_title()
    await db.set_setting("enforcement_enabled", "1")
    monkeypatch.setattr(watcher, "BACKSTOP_GRACE_S", 0)
    watcher._backstopped.clear(); watcher._first_seen.clear()
    client = AsyncMock()
    client.terminate_session = AsyncMock(return_value=True)
    await watcher._backstop(_session(), client)
    await watcher._backstop(_session(), client)
    client.terminate_session.assert_awaited_once()
    assert client.terminate_session.call_args.args[0] == "ps1"


async def test_backstop_waits_out_the_grace_period(setup_db):
    await _flagged_title()
    await db.set_setting("enforcement_enabled", "1")
    watcher._backstopped.clear(); watcher._first_seen.clear()
    client = AsyncMock()
    await watcher._backstop(_session(), client)  # first sighting: within BACKSTOP_GRACE_S
    client.terminate_session.assert_not_called()


async def test_backstop_leaves_proxied_and_unflagged_streams_alone(setup_db, monkeypatch):
    await _flagged_title()
    await db.set_setting("enforcement_enabled", "1")
    monkeypatch.setattr(watcher, "BACKSTOP_GRACE_S", 0)
    watcher._backstopped.clear(); watcher._first_seen.clear()
    await db.save_proxy_session("s1", {"rating_key": "71455", "client_id": "atv"})
    client = AsyncMock()
    await watcher._backstop(_session(), client)  # proxied
    await watcher._backstop(_session(rating_key="555", playback_session_id="ps2"), client)  # no segments
    client.terminate_session.assert_not_called()


async def test_terminate_session_calls_plex_with_the_session_and_reason():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200)

    client = PlexClient("http://plex", "tok")
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await client.terminate_session("ps1", "filtered") is True
    assert seen[0].url.path == "/status/sessions/terminate"
    assert seen[0].url.params["sessionId"] == "ps1" and seen[0].url.params["reason"] == "filtered"
    assert await client.terminate_session("", "x") is False
