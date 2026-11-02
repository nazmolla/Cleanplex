"""Unit tests for scanner.py public API — the DB-backed queue, pause, and skip functions.

These tests exercise only the lightweight state-management functions.
The NudeNet inference and frame extraction paths are not invoked here.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

import cleanplex.scanner as scanner
from cleanplex import database as db


pytestmark = pytest.mark.usefixtures("setup_db")


@pytest_asyncio.fixture(autouse=True)
async def reset_scanner_state():
    """Reset module-level state between tests (the queue itself lives in the per-test DB)."""
    scanner._current_guids.clear()
    scanner._skip_requested_guids.clear()
    scanner._paused = False
    yield
    scanner._current_guids.clear()
    scanner._skip_requested_guids.clear()
    scanner._paused = False


async def _job(guid, **kw):
    await db.upsert_scan_job(guid, kw.pop("title", guid), f"/{guid}.mkv", kw.pop("rating_key", "1"), kw.pop("library_id", "lib"), "L", **kw)


async def _queued():
    return [j["plex_guid"] for j in await db.get_queued_scan_jobs()]


# ── Queue size and enqueue ────────────────────────────────────────────────────

async def test_get_queue_size_returns_zero_initially():
    assert await scanner.get_queue_size() == 0


async def test_get_queue_size_after_enqueue():
    await _job("guid-1")
    await scanner.enqueue("guid-1")
    assert await scanner.get_queue_size() == 1


async def test_enqueue_same_guid_twice_does_not_duplicate():
    await _job("guid-dup")
    await scanner.enqueue("guid-dup")
    await scanner.enqueue("guid-dup")
    assert await _queued() == ["guid-dup"]


async def test_enqueue_multiple_distinct_guids_keeps_their_order():
    for g in ("g1", "g2", "g3"):
        await _job(g)
    await scanner.enqueue_many(["g3", "g1", "g2"])
    assert await _queued() == ["g3", "g1", "g2"]


async def test_enqueue_ignores_unknown_and_scanning_titles():
    await _job("busy")
    await db.update_scan_job_status("busy", "scanning")
    await scanner.enqueue("busy")
    await scanner.enqueue("nope")
    assert await _queued() == []


# ── force_scan_job ─────────────────────────────────────────────────────────────

async def test_force_scan_job_queues_and_flags_the_title():
    await _job("fg1")
    await scanner.force_scan_job("fg1")
    assert await _queued() == ["fg1"]
    assert (await db.get_scan_job_by_guid("fg1"))["force_scan"] == 1


async def test_force_scan_job_same_guid_twice_does_not_duplicate():
    await _job("fg2")
    await scanner.force_scan_job("fg2")
    await scanner.force_scan_job("fg2")
    assert await _queued() == ["fg2"]


async def test_force_scan_job_runs_ahead_of_titles_already_queued():
    await _job("a"); await _job("b")
    await scanner.enqueue_many(["a", "b"])
    await scanner.force_scan_job("b")
    assert await _queued() == ["b", "a"]


async def test_force_scan_job_leaves_a_scanning_title_alone():
    await _job("busy")
    await db.update_scan_job_status("busy", "scanning")
    await scanner.force_scan_job("busy")
    assert await _queued() == []


# ── Pause / resume ─────────────────────────────────────────────────────────────

def test_is_paused_initially_false():
    assert scanner.is_paused() is False


def test_pause_scanner():
    scanner.pause_scanner()
    assert scanner.is_paused() is True


def test_resume_scanner():
    scanner.pause_scanner()
    scanner.resume_scanner()
    assert scanner.is_paused() is False


# ── Current scan tracking (read from the database) ────────────────────────────

async def test_get_current_scan_returns_none_initially():
    assert await scanner.get_current_scan() is None


async def test_get_current_scans_returns_empty_initially():
    assert await scanner.get_current_scans() == []


async def test_get_current_scans_come_from_scanning_rows_sorted():
    for g in ("c", "a", "b", "idle"):
        await _job(g)
    for g in ("c", "a", "b"):
        await db.update_scan_job_status(g, "scanning")
    assert await scanner.get_current_scans() == ["a", "b", "c"]
    assert await scanner.get_current_scan() == "a"


# ── skip / request_skip_scan ──────────────────────────────────────────────────

async def test_skip_current_scan_no_op_when_nothing_scanning():
    await scanner.skip_current_scan()
    assert scanner._skip_requested_guids == set()


async def test_skip_current_scan_flags_a_scan_this_process_is_running():
    await _job("mine")
    await db.update_scan_job_status("mine", "scanning")
    scanner._current_guids.add("mine")
    await scanner.skip_current_scan()
    assert "mine" in scanner._skip_requested_guids


def test_request_skip_scan_returns_false_when_not_active():
    assert scanner.request_skip_scan("not-scanning") is False


def test_request_skip_scan_returns_true_when_active():
    scanner._current_guids.add("active-guid")
    assert scanner.request_skip_scan("active-guid") is True
    assert "active-guid" in scanner._skip_requested_guids


# ── get_worker_pool_size ───────────────────────────────────────────────────────

def test_get_worker_pool_size_returns_int():
    assert isinstance(scanner.get_worker_pool_size(), int)


# ── rebuild_queue ordering ────────────────────────────────────────────────────

async def test_rebuild_queue_movies_before_episodes():
    await _job("ep1", title="Show – S01 – E1", rating_key="10", media_type="episode", show_guid="show-a")
    await _job("mov1", title="Movie A", rating_key="5")
    await _job("ep2", title="Show – S01 – E2", rating_key="11", media_type="episode", show_guid="show-a")
    await _job("mov2", title="Movie B", rating_key="6")

    await scanner.rebuild_queue()

    queued = await _queued()
    assert set(queued[:2]) == {"mov1", "mov2"} and set(queued[2:]) == {"ep1", "ep2"}


async def test_rebuild_queue_movies_newest_first():
    await _job("old-movie", rating_key="100")
    await _job("new-movie", rating_key="999")
    await scanner.rebuild_queue()
    assert await _queued() == ["new-movie", "old-movie"]


async def test_rebuild_queue_episodes_grouped_by_show():
    await _job("a-ep1", title="ShowA – S01 – E1", rating_key="1", media_type="episode", show_guid="show-a")
    await _job("b-ep1", title="ShowB – S01 – E1", rating_key="2", media_type="episode", show_guid="show-b")
    await _job("a-ep2", title="ShowA – S01 – E2", rating_key="3", media_type="episode", show_guid="show-a")
    await _job("b-ep2", title="ShowB – S01 – E2", rating_key="4", media_type="episode", show_guid="show-b")

    await scanner.rebuild_queue()

    queued = await _queued()
    a = sorted(queued.index(g) for g in ("a-ep1", "a-ep2"))
    b = sorted(queued.index(g) for g in ("b-ep1", "b-ep2"))
    assert a[1] == a[0] + 1 and b[1] == b[0] + 1


async def test_rebuild_queue_ignored_titles_excluded():
    await _job("normal")
    await _job("ignored", rating_key="2")
    await db.set_ignored("ignored", True)
    await scanner.rebuild_queue()
    assert await _queued() == ["normal"]


async def test_rebuild_queue_twice_does_not_duplicate():
    await _job("a"); await _job("b", rating_key="2")
    await scanner.rebuild_queue()
    await scanner.rebuild_queue()
    assert await _queued() == ["b", "a"]


async def test_rebuild_queue_leaves_a_scanning_title_alone():
    await _job("busy")
    await db.update_scan_job_status("busy", "scanning")
    await scanner.rebuild_queue()
    assert await _queued() == []


# ── Model input size (issue #82) ──────────────────────────────────────────────

@pytest.mark.parametrize("name, size", [("640m", 640), (" 640M ", 640), ("320n", 320), ("", 320)])
def test_inference_resolution_follows_the_model_name(name, size):
    assert scanner._inference_resolution(name) == size


# ── force-scan jobs survive a restart (#106) ──────────────────────────────────

async def test_rebuild_queue_keeps_force_jobs_that_are_not_normally_eligible():
    """A force job whose rating is outside scan_ratings is dropped by the normal filter."""
    await db.set_setting("scan_ratings", '["R"]')
    await _job("forced", rating_key="7", content_rating="Not Rated")
    await db.set_force_scan("forced", True)
    await _job("plain", rating_key="8", content_rating="Not Rated")

    await scanner.rebuild_queue()

    assert await _queued() == ["forced"]  # normal filtering is unchanged


async def test_rebuild_queue_keeps_force_jobs_in_excluded_libraries():
    await db.set_setting("excluded_library_ids", '["lib-x"]')
    await _job("forced", rating_key="7", library_id="lib-x")
    await db.set_force_scan("forced", True)
    await scanner.rebuild_queue()
    assert await _queued() == ["forced"]


async def test_rebuild_queue_skips_ignored_force_jobs():
    await _job("forced", rating_key="7")
    await db.set_force_scan("forced", True)
    await db.set_ignored("forced", True)
    await scanner.rebuild_queue()
    assert await _queued() == []


async def test_rebuild_queue_puts_force_jobs_first():
    await _job("plain", rating_key="99")
    await _job("forced", rating_key="1")
    await db.set_force_scan("forced", True)
    await scanner.rebuild_queue()
    assert await _queued() == ["forced", "plain"]


async def test_force_job_interrupted_by_a_restart_is_requeued():
    """Scanning jobs are reset to pending at startup, keeping force_scan."""
    await _job("forced", rating_key="7")
    await db.set_force_scan("forced", True)
    await db.update_scan_job_status("forced", "scanning", progress=0.4)

    assert await db.reset_interrupted_scans() == 1
    await scanner.rebuild_queue()

    assert await _queued() == ["forced"]


# ── sync_queue: the database is the queue (#172, #174) ────────────────────────

async def test_sync_queue_adds_pending_titles_that_were_never_queued():
    await _job("new1", rating_key="5")
    await _job("new2", rating_key="6")
    assert await scanner.sync_queue() == 2
    assert await _queued() == ["new2", "new1"]


async def test_sync_queue_keeps_existing_positions_and_appends_new_ones():
    await _job("first"); await _job("second", rating_key="2")
    await scanner.enqueue_many(["first", "second"])
    await _job("late", rating_key="9")
    assert await scanner.sync_queue() == 1
    assert await _queued() == ["first", "second", "late"]


async def test_sync_queue_skips_ignored_excluded_and_filtered_titles():
    await db.set_setting("excluded_library_ids", '["lib-x"]')
    await db.set_setting("scan_ratings", '["R"]')
    await _job("ok", content_rating="R")
    await _job("ign", rating_key="2", content_rating="R"); await db.set_ignored("ign", True)
    await _job("excl", rating_key="3", library_id="lib-x", content_rating="R")
    await _job("rated", rating_key="4", content_rating="G")
    assert await scanner.sync_queue() == 1
    assert await _queued() == ["ok"]


async def test_sync_queue_is_a_no_op_when_everything_is_queued():
    await _job("a")
    await scanner.sync_queue()
    assert await scanner.sync_queue() == 0
