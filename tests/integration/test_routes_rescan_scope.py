"""Whole-show rescans and bulk rescans by scope (#137, #138)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import cleanplex.scanner as scanner
from cleanplex import database as db

pytestmark = pytest.mark.usefixtures("setup_db")

SCAN = "cleanplex.web.routes.scanner_routes.scan_mod"


async def _job(guid, *, title="Movie", library_id="lib", status="done", version=0, rating="R", ignored=False,
               media_type="movie", show_guid="", finished="2026-10-01 10:00:00"):
    await db.upsert_scan_job(plex_guid=guid, title=title, file_path=f"/m/{guid}.mkv", rating_key="1",
                             library_id=library_id, library_title="L", content_rating=rating,
                             media_type=media_type, show_guid=show_guid)
    async with db.get_connection() as conn:
        await conn.execute("UPDATE scan_jobs SET status=?, scan_version=?, ignored=?, finished_at=? WHERE plex_guid=?",
                           (status, version, int(ignored), finished, guid))
        await conn.commit()


async def _status(guid):
    return (await db.get_scan_job_by_guid(guid))["status"]


# ── scan version ────────────────────────────────────────────────────────────────

async def test_a_finished_scan_records_the_scanner_version():
    await _job("a", status="scanning")
    await db.update_scan_job_status("a", "done", progress=1.0, scan_version=scanner.SCAN_VERSION)
    assert (await db.get_scan_job_by_guid("a"))["scan_version"] == scanner.SCAN_VERSION


async def test_a_failed_scan_does_not_claim_a_version():
    await _job("a", status="scanning", version=1)
    await db.update_scan_job_status("a", "failed", error_msg="boom", scan_version=99)
    assert (await db.get_scan_job_by_guid("a"))["scan_version"] == 1


# ── candidates by scope ─────────────────────────────────────────────────────────

async def _guids(scope, **kw):
    return sorted(j["plex_guid"] for j in await db.get_rescan_candidates(scope, current_version=3, **kw))


async def test_outdated_picks_finished_titles_below_the_current_version():
    await _job("old", version=0)
    await _job("older-ish", version=2)
    await _job("current", version=3)
    await _job("pending", status="pending", version=0)
    await _job("ignored", version=0, ignored=True)
    assert await _guids("outdated") == ["old", "older-ish"]


async def test_with_segments_ignores_titles_whose_segments_are_all_false_positives():
    await _job("has")
    await _job("none")
    await _job("fp-only")
    await db.insert_segment("has", "T", 0, 1000)
    wrong = await db.insert_segment("fp-only", "T", 0, 1000)
    await db.mark_segment_false_positive(wrong)
    assert await _guids("with_segments") == ["has"]


async def test_scanned_before_uses_the_finish_time():
    await _job("early", finished="2026-09-01 10:00:00")
    await _job("late", finished="2026-10-20 10:00:00")
    assert await _guids("scanned_before", before="2026-10-01 00:00:00") == ["early"]


async def test_a_library_limits_every_scope():
    await _job("a", library_id="one")
    await _job("b", library_id="two")
    assert await _guids("all_scanned", library_id="two") == ["b"]


async def test_scanned_before_needs_a_date_and_unknown_scopes_are_rejected():
    with pytest.raises(ValueError):
        await db.get_rescan_candidates("scanned_before", 3)
    with pytest.raises(ValueError):
        await db.get_rescan_candidates("everything", 3)


# ── POST /api/scan/rescan ───────────────────────────────────────────────────────

async def test_dry_run_counts_without_changing_anything(http_client):
    await _job("a")
    await _job("b")
    with patch(f"{SCAN}.enqueue_pending", new=AsyncMock()) as enqueue:
        resp = await http_client.post("/api/scan/rescan", json={"scope": "all_scanned", "dry_run": True})

    assert resp.json() == {"ok": True, "matched": 2, "queued": 2, "skipped_ineligible": 0, "dry_run": True}
    enqueue.assert_not_called()
    assert await _status("a") == "done"


async def test_the_real_request_queues_exactly_what_the_dry_run_counted(http_client):
    for guid in ("a", "b", "c"):
        await _job(guid)
    body = {"scope": "outdated"}
    with patch(f"{SCAN}.enqueue_pending", new=AsyncMock()) as enqueue:
        dry = (await http_client.post("/api/scan/rescan", json={**body, "dry_run": True})).json()
        real = (await http_client.post("/api/scan/rescan", json=body)).json()

    assert dry["queued"] == real["queued"] == 3
    enqueue.assert_awaited_once()
    assert {await _status(g) for g in "abc"} == {"pending"}


async def test_it_uses_the_scheduled_queue_never_the_force_queue(http_client):
    await _job("a")
    with patch(f"{SCAN}.enqueue_pending", new=AsyncMock()), patch(f"{SCAN}.force_scan_job", new=AsyncMock()) as force:
        await http_client.post("/api/scan/rescan", json={"scope": "all_scanned"})
    force.assert_not_called()


async def test_titles_outside_the_rating_filter_are_reported_not_queued(http_client):
    await db.set_setting("scan_ratings", '["R"]')
    await _job("r", rating="R")
    await _job("g", rating="G")
    with patch(f"{SCAN}.enqueue_pending", new=AsyncMock()):
        resp = await http_client.post("/api/scan/rescan", json={"scope": "all_scanned"})

    assert (resp.json()["queued"], resp.json()["skipped_ineligible"]) == (1, 1)
    assert await _status("g") == "done"


async def test_scanning_and_pending_titles_are_never_reset(http_client):
    await _job("busy", status="scanning")
    await _job("waiting", status="pending")
    with patch(f"{SCAN}.enqueue_pending", new=AsyncMock()):
        resp = await http_client.post("/api/scan/rescan", json={"scope": "all_scanned"})
    assert resp.json()["matched"] == 0
    assert await _status("busy") == "scanning"


@pytest.mark.parametrize("body", [{"scope": "scanned_before"}, {"scope": "nonsense"},
                                  {"scope": "scanned_before", "before": "not-a-date"}])
async def test_bad_requests_are_422(http_client, body):
    assert (await http_client.post("/api/scan/rescan", json=body)).status_code == 422


async def test_scanned_before_over_http(http_client):
    await _job("early", finished="2026-09-01 10:00:00")
    await _job("late", finished="2026-10-20 10:00:00")
    resp = await http_client.post("/api/scan/rescan", json={"scope": "scanned_before", "before": "2026-10-01",
                                                            "dry_run": True})
    assert resp.json()["matched"] == 1


# ── POST /api/scan/show ─────────────────────────────────────────────────────────

async def _show(count=3, show_guid="show-1", title="The Show"):
    for i in range(count):
        await _job(f"{show_guid}-e{i}", title=f"{title} – Season 1 – Ep {i}", media_type="episode",
                   show_guid=show_guid)


async def test_the_whole_show_is_queued_including_episodes_without_segments(http_client):
    await _show(3)
    await db.insert_segment("show-1-e0", "T", 0, 1000)  # only one episode has a segment
    with patch(f"{SCAN}.enqueue", new=AsyncMock()) as enqueue:
        resp = await http_client.post("/api/scan/show", json={"show_guid": "show-1"})

    assert resp.json() == {"ok": True, "queued": 3, "already_scanning": 0}
    assert sorted(c.args[0] for c in enqueue.await_args_list) == ["show-1-e0", "show-1-e1", "show-1-e2"]


async def test_ignored_and_running_episodes_are_left_alone(http_client):
    await _show(3)
    await _job("show-1-ign", title="The Show – Season 1 – Ign", media_type="episode", show_guid="show-1", ignored=True)
    async with db.get_connection() as conn:
        await conn.execute("UPDATE scan_jobs SET status='scanning' WHERE plex_guid='show-1-e1'")
        await conn.commit()
    with patch(f"{SCAN}.enqueue", new=AsyncMock()) as enqueue:
        resp = await http_client.post("/api/scan/show", json={"show_guid": "show-1"})

    assert resp.json()["queued"] == 2 and resp.json()["already_scanning"] == 1
    assert "show-1-ign" not in [c.args[0] for c in enqueue.await_args_list]
    assert await _status("show-1-e1") == "scanning"


async def test_episodes_without_a_show_guid_are_found_by_title(http_client):
    for i in range(2):
        await _job(f"x{i}", title=f"No Guid Show – Season 2 – E{i}", media_type="episode", library_id="lib")
    await _job("other", title="No Guid Show Two – Season 1 – E1", media_type="episode", library_id="lib")
    with patch(f"{SCAN}.enqueue", new=AsyncMock()) as enqueue:
        resp = await http_client.post("/api/scan/show", json={"show_title": "No Guid Show", "library_id": "lib"})

    assert resp.json()["queued"] == 2
    assert sorted(c.args[0] for c in enqueue.await_args_list) == ["x0", "x1"]


async def test_a_title_with_sql_wildcards_matches_literally(http_client):
    await _job("p", title="100% Wolf – Season 1 – E1", media_type="episode")
    await _job("q", title="100 Wolf – Season 1 – E1", media_type="episode")
    with patch(f"{SCAN}.enqueue", new=AsyncMock()) as enqueue:
        await http_client.post("/api/scan/show", json={"show_title": "100% Wolf"})
    assert [c.args[0] for c in enqueue.await_args_list] == ["p"]


async def test_now_forces_the_show(http_client):
    await _show(2)
    with patch(f"{SCAN}.force_scan_job", new=AsyncMock()) as force:
        await http_client.post("/api/scan/show", json={"show_guid": "show-1", "now": True})
    assert force.await_count == 2


async def test_unknown_show_is_404_and_a_request_naming_nothing_is_422(http_client):
    assert (await http_client.post("/api/scan/show", json={"show_guid": "nope"})).status_code == 404
    assert (await http_client.post("/api/scan/show", json={})).status_code == 422


async def test_resetting_more_titles_than_one_chunk_resets_them_all():
    guids = [f"t{i}" for i in range(1200)]
    for g in guids:
        await db.upsert_scan_job(plex_guid=g, title=g, file_path="/m.mkv", rating_key="1", library_id="l", library_title="L")
    assert await db.reset_scan_jobs(guids) == 1200
