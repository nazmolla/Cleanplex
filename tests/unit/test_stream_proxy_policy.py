"""The conditions deciding whether a stream is edited (#132)."""

from __future__ import annotations

import httpx
import pytest

from cleanplex import database as db
from cleanplex import watcher
from cleanplex.plex_client import ActiveSession
from cleanplex.stream_proxy import (
    PlexTvUsers, Policy, db_filter_enabled, decide, eligible_ranges, is_lan_address,
    lan_client_controllable,
)


def _policy(**over):
    calls = []

    async def controllable(client_id, ip):
        calls.append("client")
        return over.get("controllable", False)

    async def resolve(token):
        calls.append("user")
        return over.get("user", "alice")

    async def enabled(username):
        calls.append("filter")
        return over.get("enabled", True)

    async def ranges_for(rating_key, username):
        calls.append("ranges")
        return over.get("ranges", [(1_000, 2_000)])

    policy = Policy(ranges_for=ranges_for, client_controllable=controllable, resolve_user=resolve,
                    filter_enabled=enabled, unknown_user_filtered=over.get("unknown_filtered", True))
    return policy, calls


async def test_all_three_conditions_inject_and_carry_the_ranges_and_user():
    policy, _ = _policy()
    d = await decide(policy, "42", "tv", "93.184.216.34", "tok")
    assert (d.inject, d.ranges, d.username) == (True, [(1_000, 2_000)], "alice")


async def test_a_client_live_skipping_reaches_stops_before_any_lookup():
    policy, calls = _policy(controllable=True)
    d = await decide(policy, "42", "tv", "192.168.1.5", "tok")
    assert d.inject is False and calls == ["client"]


async def test_filtering_off_stops_before_the_segment_lookup():
    policy, calls = _policy(enabled=False)
    d = await decide(policy, "42", "tv", "93.184.216.34", "tok")
    assert (d.inject, d.reason) == (False, "filtering is off for alice")
    assert "ranges" not in calls


async def test_no_eligible_segments_leaves_the_stream_alone():
    policy, _ = _policy(ranges=[])
    d = await decide(policy, "42", "tv", "93.184.216.34", "tok")
    assert (d.inject, d.reason) == (False, "no eligible segments for this title")


async def test_unidentified_viewer_is_filtered_by_default():
    policy, _ = _policy(user=None)
    assert (await decide(policy, "42", "tv", "93.184.216.34", "tok")).inject is True


async def test_unidentified_viewer_can_be_left_alone_by_choice():
    policy, _ = _policy(user=None, unknown_filtered=False)
    d = await decide(policy, "42", "tv", "93.184.216.34", "tok")
    assert (d.inject, d.reason) == (False, "viewer could not be identified")


async def test_unchecked_conditions_are_skipped():
    async def ranges_for(rating_key, username):
        return [(0, 1)]

    d = await decide(Policy(ranges_for=ranges_for), "42", "", "", "")
    assert d.inject is True


@pytest.mark.parametrize("host, lan", [
    ("192.168.1.20", True), ("10.0.0.4", True), ("172.20.1.1", True), ("127.0.0.1", True),
    ("169.254.3.3", True), ("fd00::5", True),
    ("8.8.8.8", False), ("93.184.216.34", False), ("2001:4860:4860::8888", False), ("", False), ("not-an-ip", False),
])
def test_lan_address_detection(host, lan):
    assert is_lan_address(host) is lan


@pytest.mark.usefixtures("setup_db")
async def test_a_recorded_client_overrides_the_address_guess():
    await db.record_client_control("iphone", False)
    await db.record_client_control("appletv", True)
    assert await lan_client_controllable("iphone", "192.168.1.30") is False  # local but not reachable
    assert await lan_client_controllable("appletv", "93.184.216.34") is True


@pytest.mark.usefixtures("setup_db")
async def test_an_unseen_client_is_guessed_from_its_address():
    assert await lan_client_controllable("new", "192.168.1.30") is True
    assert await lan_client_controllable("new", "93.184.216.34") is False
    assert await lan_client_controllable("", "192.168.1.30") is True


@pytest.mark.usefixtures("setup_db")
async def test_filter_enabled_defaults_on_and_follows_the_user_record():
    assert await db_filter_enabled("nobody") is True
    await db.upsert_user_filter("kid", False)
    assert await db_filter_enabled("kid") is False
    await db.upsert_user_filter("kid", True)
    assert await db_filter_enabled("kid") is True


# ── eligible ranges follow the viewer's own levels and actions ──────────────────

async def _title():
    await db.upsert_scan_job(plex_guid="g", title="T", file_path="/m.mkv", rating_key="7",
                             library_id="l", library_title="L")
    await db.insert_segment("g", "T", 10_000, 20_000, labels="MALE_GENITALIA_EXPOSED", severity="high")
    await db.insert_segment("g", "T", 40_000, 50_000, labels="FEMALE_BREAST_EXPOSED", severity="medium")
    await db.insert_segment("g", "T", 70_000, 80_000, labels="FEMALE_BREAST_COVERED", severity="low")
    await db.insert_segment("g", "T", 90_000, 91_000, category="language", action="mute", severity="high")


@pytest.mark.usefixtures("setup_db")
async def test_default_viewer_gets_every_skip_segment_but_not_mutes():
    await _title()
    got = await eligible_ranges(1_000, 2_000)("7", "alice")
    assert got == [(9_000, 22_000), (39_000, 52_000), (69_000, 82_000)]


@pytest.mark.usefixtures("setup_db")
@pytest.mark.parametrize("level, count", [(3, 3), (2, 2), (1, 1), (0, 0)])
async def test_the_nudity_level_decides_which_segments_are_cut(level, count):
    await _title()
    await db.upsert_user_category_pref("alice", "nudity", level, "")
    assert len(await eligible_ranges(0, 0)("7", "alice")) == count


@pytest.mark.usefixtures("setup_db")
async def test_a_users_skip_override_turns_a_mute_into_a_cut():
    await _title()
    await db.upsert_user_category_pref("alice", "language", 3, "skip")
    got = await eligible_ranges(0, 0)("7", "alice")
    assert (90_000, 91_000) in got


@pytest.mark.usefixtures("setup_db")
async def test_a_title_without_segments_has_no_ranges():
    assert await eligible_ranges()("999", "alice") == []


# ── plex.tv user lookup ──────────────────────────────────────────────────────────

async def test_plex_tv_user_is_resolved_and_cached():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["x-plex-token"])
        return httpx.Response(200, json={"username": "MoNazmi", "title": "Mohamed"})

    users = PlexTvUsers(transport=httpx.MockTransport(handler))
    assert await users("tok") == "MoNazmi"
    assert await users("tok") == "MoNazmi"
    assert seen == ["tok"]
    await users.aclose()


async def test_a_managed_user_without_a_username_falls_back_to_the_title():
    users = PlexTvUsers(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"username": "", "title": "Kid"})))
    assert await users("tok") == "Kid"
    await users.aclose()


async def test_a_failed_lookup_returns_none_and_is_not_cached():
    answers = iter([httpx.Response(401), httpx.Response(200, json={"username": "late"})])
    users = PlexTvUsers(transport=httpx.MockTransport(lambda r: next(answers)))
    assert await users("tok") is None
    assert await users("tok") == "late"
    await users.aclose()


# ── watcher records reachability ────────────────────────────────────────────────

def _session(client_id="tv", controllable=True, local=True):
    return ActiveSession(session_key="1", user="u", title="t", full_title="t", plex_guid="g", rating_key="7",
                         media_type="movie", position_ms=0, duration_ms=1, client_identifier=client_id,
                         client_title="TV", is_controllable=controllable, client_local=local)


@pytest.mark.usefixtures("setup_db")
async def test_watcher_records_reachable_only_for_a_controllable_local_player(monkeypatch):
    watcher._recorded_control.clear()
    await watcher._remember_client_control(_session("a", controllable=True, local=True))
    await watcher._remember_client_control(_session("b", controllable=True, local=False))
    await watcher._remember_client_control(_session("c", controllable=False, local=True))
    assert [await db.get_client_control(c) for c in "abc"] == [True, False, False]


@pytest.mark.usefixtures("setup_db")
async def test_watcher_writes_only_when_the_value_changes():
    watcher._recorded_control.clear()
    writes = []
    real = db.record_client_control

    async def counting(client_id, controllable):
        writes.append((client_id, controllable))
        await real(client_id, controllable)

    db_record = db.record_client_control
    db.record_client_control = counting
    try:
        await watcher._remember_client_control(_session("a"))
        await watcher._remember_client_control(_session("a"))
        await watcher._remember_client_control(_session("a", controllable=False))
    finally:
        db.record_client_control = db_record
    assert writes == [("a", True), ("a", False)]


@pytest.mark.usefixtures("setup_db")
async def test_watcher_ignores_a_session_with_no_client_identifier():
    watcher._recorded_control.clear()
    await watcher._remember_client_control(_session(""))
    assert await db.get_client_control("") is None
