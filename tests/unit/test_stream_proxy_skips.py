"""Skip events recorded by the stream proxy, and client reachability learned from command outcomes (#184)."""

import pytest

from cleanplex import database as db
from cleanplex.plex_client import PlexClient
from cleanplex.stream_proxy import _Session, cut_boundaries, record_cut_played

pytestmark = pytest.mark.usefixtures("setup_db")


def test_cut_boundaries_number_each_run_of_removed_segments():
    assert cut_boundaries([331, 332, 333, 4480, 4481, 6705]) == {334: 0, 4482: 1, 6706: 2}
    assert cut_boundaries([]) == {}


async def _title_with_segments():
    await db.upsert_scan_job("g1", "Film", "/f.mkv", "71455", "1", "Movies")
    high = await db.insert_segment("g1", "Film", 334_000, 350_000, 0.9, "", "FEMALE_BREAST_EXPOSED",
                                   category="nudity", severity="high")
    low = await db.insert_segment("g1", "Film", 900_000, 910_000, 0.9, "", "FEMALE_BREAST_COVERED",
                                  category="nudity", severity="low")
    return high, low


async def test_record_cut_played_logs_one_skip_event_per_segment_inside_the_cut():
    high, _ = await _title_with_segments()
    session = _Session("71455", 0, username="alice", client_id="c1", client_title="Chrome")

    recorded = await record_cut_played(session, (331_000, 353_000))

    rows = await db.get_skip_events()
    assert recorded == 1  # the other segment lies outside the cut
    assert (rows[0]["segment_id"], rows[0]["client_title"], rows[0]["username"], rows[0]["success"]) == (high, "Chrome", "alice", 1)
    assert (rows[0]["position_ms"], rows[0]["target_ms"], rows[0]["category"]) == (331_000, 353_000, "nudity")


async def test_record_cut_played_skips_segments_the_viewers_level_would_not_act_on():
    await _title_with_segments()
    await db.upsert_user_category_pref("bob", "nudity", 0)  # level 0 + high severity does not exceed 3

    assert await record_cut_played(_Session("71455", 0, username="bob"), (331_000, 353_000)) == 0
    assert await db.get_skip_events() == []


async def test_record_cut_played_returns_zero_for_an_unknown_title():
    assert await record_cut_played(_Session("999", 0, username="alice"), (0, 10_000)) == 0


async def test_a_failed_command_marks_the_client_unreachable_and_a_success_marks_it_reachable():
    client = PlexClient("http://x", "t")

    async def fail(*a, **k):
        return False

    async def ok(*a, **k):
        return True

    client._send_command_inner = fail
    assert await client._send_command("/p", "web-1") is False
    assert await db.get_client_control("web-1") is False

    client._send_command_inner = ok
    assert await client._send_command("/p", "web-1") is True
    assert await db.get_client_control("web-1") is True


async def test_plex_web_is_always_edited_even_when_the_registry_says_it_is_reachable():
    from cleanplex.stream_proxy import Policy, decide

    async def reachable(client_id, ip):
        return True

    async def ranges(key, user):
        return [(1000, 2000)]

    policy = Policy(ranges_for=ranges, client_controllable=reachable)

    web = await decide(policy, "1", "c", "192.168.0.5", "", "Plex Web")
    tv = await decide(policy, "1", "c", "192.168.0.5", "", "Plex for Apple TV")

    assert web.inject is True and tv.inject is False
