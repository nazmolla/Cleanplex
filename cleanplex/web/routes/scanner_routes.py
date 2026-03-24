from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...logger import get_logger
import cleanplex.plex_client as plex_mod
from ... import database as db
from ... import scanner as scan_mod

logger = get_logger(__name__)
router = APIRouter(prefix="/api/scan", tags=["scanner"])


class ScanTitleRequest(BaseModel):
    """Request body for scanning a title."""
    plex_guid: str
    now: bool = False
    library_id: str | None = None


class ScanLibraryRequest(BaseModel):
    """Request body for scanning a library."""
    now: bool = False


@router.get("/queue")
async def get_scan_queue():
    jobs = await db.get_scan_jobs()
    return {
        "jobs": jobs,
        "queue_size": scan_mod.get_queue_size(),
        "current": scan_mod.get_current_scan(),
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
    return {"ok": True, "queued": plex_guid}


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
                    )
            jobs = await db.get_scan_jobs_by_library(library_id)
        except RuntimeError:
            return {"ok": False, "error": "Plex not configured"}

    queued = 0
    for job in jobs:
        if job["status"] in ("pending", "failed"):
            await db.reset_scan_job(job["plex_guid"])
            if body.now:
                await scan_mod.force_scan_job(job["plex_guid"])
            else:
                await scan_mod.enqueue(job["plex_guid"])
            queued += 1

    return {"ok": True, "queued": queued}


@router.post("/pause")
async def pause_scanner():
    scan_mod.pause_scanner()
    return {"ok": True, "paused": True}


@router.post("/resume")
async def resume_scanner():
    scan_mod.resume_scanner()
    return {"ok": True, "paused": False}


@router.post("/skip-current")
async def skip_current_scan():
    """Skip (abort) the title currently being scanned; it stays pending for the next window."""
    if not scan_mod.get_current_scan():
        raise HTTPException(status_code=404, detail="No scan in progress")
    scan_mod.skip_current_scan()
    return {"ok": True}
