"""Unit tests for scanner.py public API — queue, pause, and skip functions.

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
    """Drain queues and reset module-level state between tests."""
    # Drain queues
    while not scanner._scan_queue.empty():
        scanner._scan_queue.get_nowait()
    while not scanner._force_scan_queue.empty():
        scanner._force_scan_queue.get_nowait()
    scanner._queued_normal.clear()
    scanner._queued_force.clear()
    scanner._current_guids.clear()
    scanner._skip_requested_guids.clear()
    scanner._paused = False
    yield
    # Drain again after test
    while not scanner._scan_queue.empty():
        scanner._scan_queue.get_nowait()
    while not scanner._force_scan_queue.empty():
        scanner._force_scan_queue.get_nowait()
    scanner._queued_normal.clear()
    scanner._queued_force.clear()
    scanner._current_guids.clear()
    scanner._skip_requested_guids.clear()
    scanner._paused = False


# ── Queue size ─────────────────────────────────────────────────────────────────

def test_get_queue_size_returns_zero_initially():
    assert scanner.get_queue_size() == 0


async def test_get_queue_size_after_enqueue():
    await scanner.enqueue("guid-1")
    assert scanner.get_queue_size() == 1


async def test_enqueue_same_guid_twice_does_not_duplicate():
    await scanner.enqueue("guid-dup")
    await scanner.enqueue("guid-dup")
    assert scanner.get_queue_size() == 1


async def test_enqueue_multiple_distinct_guids():
    await scanner.enqueue("g1")
    await scanner.enqueue("g2")
    await scanner.enqueue("g3")
    assert scanner.get_queue_size() == 3


# ── force_scan_job ─────────────────────────────────────────────────────────────

async def test_force_scan_job_adds_to_force_queue():
    await db.upsert_scan_job("fg1", "T", "/f.mkv", "1", "lib1", "L")
    await scanner.force_scan_job("fg1")
    assert scanner._force_scan_queue.qsize() == 1


async def test_force_scan_job_same_guid_twice_does_not_duplicate():
    await db.upsert_scan_job("fg2", "T", "/f.mkv", "1", "lib1", "L")
    await scanner.force_scan_job("fg2")
    await scanner.force_scan_job("fg2")
    assert scanner._force_scan_queue.qsize() == 1


async def test_force_scan_job_moves_from_normal_queue():
    await scanner.enqueue("fg3")
    assert scanner._scan_queue.qsize() == 1
    await db.upsert_scan_job("fg3", "T", "/f.mkv", "1", "lib1", "L")
    await scanner.force_scan_job("fg3")
    # Should no longer be in normal queue set
    assert "fg3" not in scanner._queued_normal
    assert "fg3" in scanner._queued_force


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


# ── Current scan tracking ──────────────────────────────────────────────────────

def test_get_current_scan_returns_none_initially():
    assert scanner.get_current_scan() is None


def test_get_current_scans_returns_empty_initially():
    assert scanner.get_current_scans() == []


def test_get_current_scan_returns_first_alphabetically():
    scanner._current_guids.update({"z-guid", "a-guid", "m-guid"})
    result = scanner.get_current_scan()
    assert result == "a-guid"


def test_get_current_scans_returns_sorted():
    scanner._current_guids.update({"c", "a", "b"})
    assert scanner.get_current_scans() == ["a", "b", "c"]


# ── skip / request_skip_scan ──────────────────────────────────────────────────

def test_skip_current_scan_no_op_when_nothing_scanning():
    # Should not raise; just a no-op
    scanner.skip_current_scan()


def test_request_skip_scan_returns_false_when_not_active():
    result = scanner.request_skip_scan("not-scanning")
    assert result is False


def test_request_skip_scan_returns_true_when_active():
    scanner._current_guids.add("active-guid")
    result = scanner.request_skip_scan("active-guid")
    assert result is True
    assert "active-guid" in scanner._skip_requested_guids


# ── get_worker_pool_size ───────────────────────────────────────────────────────

def test_get_worker_pool_size_returns_int():
    assert isinstance(scanner.get_worker_pool_size(), int)


# ── enqueue_pending ordering ───────────────────────────────────────────────────

async def test_enqueue_pending_movies_before_episodes():
    """All movie jobs must be enqueued before any TV episode jobs."""
    await db.upsert_scan_job("ep1", "Show – S01 – E1", "/e1.mkv", "10", "lib", "L", media_type="episode", show_guid="show-a")
    await db.upsert_scan_job("mov1", "Movie A", "/m1.mkv", "5", "lib", "L", media_type="movie")
    await db.upsert_scan_job("ep2", "Show – S01 – E2", "/e2.mkv", "11", "lib", "L", media_type="episode", show_guid="show-a")
    await db.upsert_scan_job("mov2", "Movie B", "/m2.mkv", "6", "lib", "L", media_type="movie")

    await scanner.enqueue_pending()

    queued = []
    while not scanner._scan_queue.empty():
        queued.append(scanner._scan_queue.get_nowait())

    assert queued.index("mov1") < queued.index("ep1")
    assert queued.index("mov2") < queued.index("ep1")
    assert queued.index("mov1") < queued.index("ep2")
    assert queued.index("mov2") < queued.index("ep2")


async def test_enqueue_pending_movies_newest_first():
    """Within movies, higher rating_key (more recently added) comes first."""
    await db.upsert_scan_job("old-movie", "Old Film", "/old.mkv", "100", "lib", "L", media_type="movie")
    await db.upsert_scan_job("new-movie", "New Film", "/new.mkv", "999", "lib", "L", media_type="movie")

    await scanner.enqueue_pending()

    queued = []
    while not scanner._scan_queue.empty():
        queued.append(scanner._scan_queue.get_nowait())

    assert queued.index("new-movie") < queued.index("old-movie")


async def test_enqueue_pending_episodes_grouped_by_show():
    """Episodes from the same show must be contiguous — no interleaving between shows."""
    await db.upsert_scan_job("a-ep1", "ShowA – S01 – E1", "/a1.mkv", "1", "lib", "L", media_type="episode", show_guid="show-a")
    await db.upsert_scan_job("b-ep1", "ShowB – S01 – E1", "/b1.mkv", "2", "lib", "L", media_type="episode", show_guid="show-b")
    await db.upsert_scan_job("a-ep2", "ShowA – S01 – E2", "/a2.mkv", "3", "lib", "L", media_type="episode", show_guid="show-a")
    await db.upsert_scan_job("b-ep2", "ShowB – S01 – E2", "/b2.mkv", "4", "lib", "L", media_type="episode", show_guid="show-b")

    await scanner.enqueue_pending()

    queued = []
    while not scanner._scan_queue.empty():
        queued.append(scanner._scan_queue.get_nowait())

    idx_a1, idx_a2 = queued.index("a-ep1"), queued.index("a-ep2")
    idx_b1, idx_b2 = queued.index("b-ep1"), queued.index("b-ep2")

    # No show-B episode should appear between show-A episodes and vice-versa
    assert max(idx_a1, idx_a2) < min(idx_b1, idx_b2) or max(idx_b1, idx_b2) < min(idx_a1, idx_a2)


async def test_enqueue_pending_ignored_titles_excluded():
    """Ignored jobs must not be enqueued."""
    await db.upsert_scan_job("normal", "Keep Me", "/k.mkv", "1", "lib", "L")
    await db.upsert_scan_job("ignored", "Skip Me", "/s.mkv", "2", "lib", "L")
    await db.set_ignored("ignored", True)

    await scanner.enqueue_pending()

    queued = []
    while not scanner._scan_queue.empty():
        queued.append(scanner._scan_queue.get_nowait())

    assert "normal" in queued
    assert "ignored" not in queued
