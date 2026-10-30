import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ...logger import get_logger
import cleanplex.plex_client as plex_mod
from ... import database as db
from ... import events
from ... import scanner as scan_mod

logger = get_logger(__name__)
router = APIRouter(prefix="/api/scan", tags=["scanner"])


class ScanTitleRequest(BaseModel):
    """Request body for scanning a title."""
    plex_guid: str
    now: bool = False
    library_id: str | None = None


class ScanTitlesRequest(BaseModel):
    """Request body for rescanning many titles at once (a season or a whole show)."""
    # Bounded so one request cannot pin the event loop; a long-running show is
    # well under this.
    plex_guids: list[str] = Field(min_length=1, max_length=5000)
    now: bool = False


class ScanShowRequest(BaseModel):
    """Request body for rescanning every episode of a show, with or without detected segments."""
    show_guid: str = ""
    # Fallback for episodes that carry no show GUID: match "<show> – ..." titles in the library.
    show_title: str = ""
    # One season of the show, as it appears in titles ("Season 1"); needs show_title.
    season: str = ""
    library_id: str | None = None
    now: bool = False


class BulkRescanRequest(BaseModel):
    """Request body for rescanning many titles chosen by scope rather than by name (#138)."""
    scope: Literal["outdated", "with_segments", "all_scanned", "scanned_before", "whole_library", "failed"]
    library_id: str | None = None
    before: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="YYYY-MM-DD, UTC; for scanned_before")
    dry_run: bool = False


class ScanLibraryRequest(BaseModel):
    """Request body for scanning a library."""
    now: bool = False


class SkipCurrentScanRequest(BaseModel):
    plex_guid: str | None = None


@router.get("/queue")
async def get_scan_queue(
    status: str | None = Query(None, description="Comma-separated job statuses; default is everything except done"),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
):
    """Return a page of scan jobs (finished ones excluded by default) with per-status counts (#156)."""
    counts = await db.count_scan_jobs_by_status()
    wanted = [x.strip() for x in status.split(",") if x.strip()] if status else [s for s in counts if s != "done"]
    jobs = await db.get_scan_jobs_page(wanted, limit, offset)
    return {
        "jobs": jobs,
        "counts": counts,
        "queue_size": scan_mod.get_queue_size(),
        "current": scan_mod.get_current_scan(),
        "currents": scan_mod.get_current_scans(),
        "paused": scan_mod.is_paused(),
    }


@router.post("/title")
async def scan_title(body: ScanTitleRequest):
    """Queue a single title for scanning. If now=true, move to front regardless of time window."""
    plex_guid = body.plex_guid
    job = await db.get_scan_job_by_guid(plex_guid)
    if not job:
        # Try to find and create the job from Plex
        if body.library_id:
            try:
                client = plex_mod.get_client()
                logger.info(f"Fetching library items for library_id={body.library_id}")
                items = await client.get_library_items(body.library_id)
                logger.info(f"Got {len(items)} items from library")
                item = next((i for i in items if i.plex_guid == plex_guid), None)
                if item:
                    logger.info(f"Found title {item.title}, creating scan job")
                    await db.upsert_scan_job(
                        plex_guid=item.plex_guid,
                        title=item.title,
                        file_path=item.file_path,
                        rating_key=item.rating_key,
                        library_id=item.library_id,
                        library_title=item.library_title,
                        content_rating=item.content_rating,
                        media_type=item.media_type,
                        year=item.year,
                        part_files=json.dumps(item.part_files),
                    )
                    job = await db.get_scan_job_by_guid(plex_guid)
                    logger.info(f"Scan job created for {plex_guid}")
                else:
                    logger.warning(f"Title {plex_guid} not found in library items")
            except RuntimeError as exc:
                logger.error(f"Plex client error: {exc}")
            except Exception as exc:
                logger.error(f"Error creating scan job: {exc}")
        if not job:
            logger.warning(f"No scan job found for {plex_guid}")
            raise HTTPException(status_code=404, detail="Title not found")

    logger.info(f"Queueing title {plex_guid} for scan (now={body.now})")
    await db.reset_scan_job(plex_guid)
    if body.now:
        logger.info(f"Force-scanning {plex_guid} immediately")
        await scan_mod.force_scan_job(plex_guid)
    else:
        await scan_mod.enqueue(plex_guid)
    logger.info(f"Title {plex_guid} queued. Queue size: {scan_mod.get_queue_size()}")
    events.publish("scanner_changed")
    return {"ok": True, "queued": plex_guid}


async def _queue_rescan(guids: list[str], now: bool) -> None:
    """Reset the jobs and queue them: now jumps the queue, otherwise they wait for the scan window."""
    await db.reset_scan_jobs(guids)
    for guid in guids:
        if now:
            await scan_mod.force_scan_job(guid)
        else:
            await scan_mod.enqueue(guid)
    events.publish("scanner_changed")


@router.post("/titles")
async def scan_titles(body: ScanTitlesRequest):
    """Rescan titles that already have scan jobs. now=true jumps the queue; otherwise
    they wait for the scan window. Unknown GUIDs are reported back, not created."""
    guids = list(dict.fromkeys(body.plex_guids))
    jobs = await db.get_scan_jobs_by_guids(guids)
    known = [g for g in guids if g in jobs]
    if not known:
        raise HTTPException(status_code=404, detail="None of these titles have scan jobs")

    await _queue_rescan(known, body.now)
    logger.info("Queued %d titles for rescan (now=%s)", len(known), body.now)
    return {"ok": True, "queued": len(known), "missing": [g for g in guids if g not in jobs]}


@router.post("/show")
async def scan_show(body: ScanShowRequest):
    """Rescan every non-ignored episode of a show, including episodes with no segments.

    An episode being scanned right now is left to finish rather than reset under its worker.
    """
    if not body.show_guid and not body.show_title:
        raise HTTPException(status_code=422, detail="show_guid or show_title is required")
    if body.season and not body.show_title:
        raise HTTPException(status_code=422, detail="a season needs show_title")
    jobs = await db.get_show_episode_jobs(body.show_guid, body.show_title, body.library_id, body.season)
    if not jobs:
        raise HTTPException(status_code=404, detail="No episodes of this show or season have scan jobs")
    guids = [j["plex_guid"] for j in jobs if j["status"] != "scanning"]
    await _queue_rescan(guids, body.now)
    logger.info("Queued %d episodes of '%s' for rescan (now=%s)", len(guids), body.show_title or body.show_guid, body.now)
    return {"ok": True, "queued": len(guids), "already_scanning": len(jobs) - len(guids)}


@router.post("/rescan")
async def bulk_rescan(body: BulkRescanRequest):
    """Queue every finished title matching a scope for a scheduled rescan.

    Goes through the normal queue (ordered, scan window, rating filters), never the force queue,
    so a large batch cannot crowd out a title someone asked for. dry_run reports the counts only.
    """
    if body.scope == "scanned_before" and not body.before:
        raise HTTPException(status_code=422, detail="scanned_before needs a date")
    if body.scope == "whole_library" and not body.library_id:
        raise HTTPException(status_code=422, detail="whole_library needs a library_id")
    candidates = await db.get_rescan_candidates(
        body.scope, scan_mod.SCAN_VERSION, body.library_id, f"{body.before} 00:00:00" if body.before else None
    )
    excluded = set(json.loads(await db.get_setting("excluded_library_ids", "[]")))
    ratings = set(json.loads(await db.get_setting("scan_ratings", "[]")))
    eligible = [j["plex_guid"] for j in candidates if scan_mod.is_scan_eligible(j, excluded, ratings)]
    # Failed titles that are ignored are never scanned; clearing them takes them off the Failed list.
    ignored_failed = await db.count_failed_ignored(body.library_id) if body.scope == "failed" else 0
    if not body.dry_run and body.scope == "failed" and ignored_failed:
        await db.clear_failed_ignored(body.library_id)
    if not body.dry_run and eligible:
        await db.reset_scan_jobs(eligible)
        # Rebuilds the normal queue in priority order and keeps the force queue as it is.
        await scan_mod.enqueue_pending()
        events.publish("scanner_changed")
        logger.info("Queued %d titles for a scheduled rescan (scope=%s)", len(eligible), body.scope)
    return {
        "ok": True,
        "matched": len(candidates),
        "queued": len(eligible),
        "skipped_ineligible": len(candidates) - len(eligible),
        "ignored_cleared": ignored_failed,
        "dry_run": body.dry_run,
    }


@router.post("/library/{library_id}")
async def scan_library(library_id: str, body: ScanLibraryRequest):
    """Queue all titles in a library for scanning."""
    jobs = await db.get_scan_jobs_by_library(library_id)
    if not jobs:
        # Try to discover items from Plex
        try:
            client = plex_mod.get_client()
            items = await client.get_library_items(library_id)
            for item in items:
                if item.file_path:
                    await db.upsert_scan_job(
                        plex_guid=item.plex_guid,
                        title=item.title,
                        file_path=item.file_path,
                        rating_key=item.rating_key,
                        library_id=item.library_id,
                        library_title=item.library_title,
                        content_rating=item.content_rating,
                        media_type=item.media_type,
                        year=item.year,
                        part_files=json.dumps(item.part_files),
                    )
            jobs = await db.get_scan_jobs_by_library(library_id)
        except RuntimeError:
            return {"ok": False, "error": "Plex not configured"}

    # For the normal queue, pre-filter using the same eligibility rules as enqueue_pending()
    # so ineligible titles never enter the queue rather than being silently dropped later.
    excluded_library_ids: set[str] = set()
    scan_ratings_set: set[str] = set()
    if not body.now:
        excluded_library_ids = set(json.loads(await db.get_setting("excluded_library_ids", "[]")))
        scan_ratings_set = set(json.loads(await db.get_setting("scan_ratings", "[]")))

    queued = 0
    skipped = 0
    for job in jobs:
        if job["status"] not in ("pending", "failed"):
            continue
        if body.now:
            await db.reset_scan_job(job["plex_guid"])
            await scan_mod.force_scan_job(job["plex_guid"])
            queued += 1
        elif scan_mod.is_scan_eligible(job, excluded_library_ids, scan_ratings_set):
            await db.reset_scan_job(job["plex_guid"])
            await scan_mod.enqueue(job["plex_guid"])
            queued += 1
        else:
            skipped += 1

    events.publish("scanner_changed")
    return {"ok": True, "queued": queued, "skipped_ineligible": skipped}


@router.post("/pause")
async def pause_scanner():
    scan_mod.pause_scanner()
    return {"ok": True, "paused": True}


@router.post("/resume")
async def resume_scanner():
    scan_mod.resume_scanner()
    return {"ok": True, "paused": False}


@router.post("/skip-current")
async def skip_current_scan(body: SkipCurrentScanRequest | None = None):
    """Skip (abort) the title currently being scanned; it stays pending for the next window."""
    target_guid = body.plex_guid if body and body.plex_guid else scan_mod.get_current_scan()
    if not target_guid:
        raise HTTPException(status_code=404, detail="No scan in progress")
    if body and body.plex_guid:
        if not scan_mod.request_skip_scan(body.plex_guid):
            raise HTTPException(status_code=404, detail="Requested scan is not currently active")
    else:
        scan_mod.skip_current_scan()
    events.publish("scanner_changed")
    return {"ok": True, "skipped": target_guid}


class ToggleIgnoredRequest(BaseModel):
    ignored: bool


@router.post("/restart-scanner")
async def restart_scanner():
    """Trigger a clean scanner pool restart. The pool will pick up current config
    and re-order the queue within ~10 seconds."""
    await scan_mod.request_scanner_restart()
    return {"ok": True}


@router.post("/reorder-queue")
async def reorder_queue():
    """Drain and re-enqueue all pending jobs in priority order (movies newest-first,
    then TV episodes grouped by show). Use this to fix queue ordering at any time
    without restarting the server."""
    await scan_mod.enqueue_pending()
    events.publish("scanner_changed")
    return {"ok": True, "queue_size": scan_mod.get_queue_size()}


@router.post("/title/{plex_guid:path}/ignore")
async def toggle_title_ignored(plex_guid: str, body: ToggleIgnoredRequest):
    """Mark a title as ignored (will be skipped during scanning) or re-enable it."""
    job = await db.get_scan_job_by_guid(plex_guid)
    if not job:
        raise HTTPException(status_code=404, detail="Title not found")
    
    await db.set_ignored(plex_guid, body.ignored)
    events.publish("titles_changed", library_id=job.get("library_id") or "")
    return {"ok": True, "plex_guid": plex_guid, "ignored": body.ignored}
