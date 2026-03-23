from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...logger import get_logger
import cleanplex.plex_client as plex_mod
from ... import database as db
from ... import scanner as scan_mod

logger = get_logger(__name__)
router = APIRouter(prefix="/api/scan", tags=["scanner"])


@router.get("/queue")
async def get_scan_queue():
    jobs = await db.get_scan_jobs()
    return {
        "jobs": jobs,
        "queue_size": scan_mod.get_queue_size(),
        "current": scan_mod.get_current_scan(),
        "paused": scan_mod.is_paused(),
    }


@router.post("/title/{plex_guid}")
async def scan_title(plex_guid: str, now: bool = False):
    """Queue a single title for scanning. If now=true, move to front regardless of time window."""
    job = await db.get_scan_job_by_guid(plex_guid)
    if not job:
        raise HTTPException(status_code=404, detail="Title not found in scan jobs")

    await db.reset_scan_job(plex_guid)
    if now:
        scan_mod.resume_scanner()
    await scan_mod.enqueue(plex_guid)
    return {"ok": True, "queued": plex_guid}


@router.post("/library/{library_id}")
async def scan_library(library_id: str, now: bool = False):
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
                    )
            jobs = await db.get_scan_jobs_by_library(library_id)
        except RuntimeError:
            return {"ok": False, "error": "Plex not configured"}

    queued = 0
    for job in jobs:
        if job["status"] in ("pending", "failed"):
            await db.reset_scan_job(job["plex_guid"])
            await scan_mod.enqueue(job["plex_guid"])
            queued += 1

    if now:
        scan_mod.resume_scanner()

    return {"ok": True, "queued": queued}


@router.post("/pause")
async def pause_scanner():
    scan_mod.pause_scanner()
    return {"ok": True, "paused": True}


@router.post("/resume")
async def resume_scanner():
    scan_mod.resume_scanner()
    return {"ok": True, "paused": False}
