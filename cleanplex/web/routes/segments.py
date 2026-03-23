from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...logger import get_logger
import cleanplex.plex_client as plex_mod
from ... import database as db

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["segments"])


# ── Libraries / titles tree ───────────────────────────────────────────────────

@router.get("/libraries")
async def get_libraries():
    """Return all Plex library sections."""
    try:
        client = plex_mod.get_client()
        sections = await client.get_library_sections()
        return {
            "libraries": [
                {"id": s.section_id, "title": s.title, "type": s.section_type}
                for s in sections
            ]
        }
    except RuntimeError:
        return {"libraries": [], "error": "Plex not configured"}


@router.get("/libraries/{library_id}/titles")
async def get_titles_in_library(library_id: str):
    """Return all scan jobs (titles) for a given library, with Plex poster URLs."""
    jobs = await db.get_scan_jobs_by_library(library_id)
    try:
        client = plex_mod.get_client()
    except RuntimeError:
        client = None

    result = []
    for job in jobs:
        thumb_url = ""
        if client and job.get("rating_key"):
            thumb_url = client.thumb_url(f"/library/metadata/{job['rating_key']}/thumb")
        result.append({
            "plex_guid": job["plex_guid"],
            "rating_key": job.get("rating_key", ""),
            "title": job["title"],
            "status": job["status"],
            "progress": job["progress"],
            "thumb_url": thumb_url,
            "segment_count": len(await db.get_segments_for_guid(job["plex_guid"])),
        })
    return {"titles": result}


@router.get("/titles/{plex_guid}/segments")
async def get_segments_for_title(plex_guid: str):
    """Return all segments for a specific title."""
    segments = await db.get_segments_for_guid(plex_guid)
    result = []
    for seg in segments:
        result.append({
            "id": seg["id"],
            "plex_guid": seg["plex_guid"],
            "title": seg["title"],
            "start_ms": seg["start_ms"],
            "end_ms": seg["end_ms"],
            "confidence": seg["confidence"],
            "has_thumbnail": bool(seg.get("thumbnail_path")),
            "thumbnail_url": f"/api/thumbnails/{seg['id']}" if seg.get("thumbnail_path") else "",
            "created_at": seg["created_at"],
        })
    return {"segments": result}


@router.delete("/segments/{segment_id}")
async def delete_segment(segment_id: int):
    deleted = await db.delete_segment(segment_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Segment not found")
    return {"ok": True}


@router.get("/segments")
async def get_all_segments(limit: int = 100, offset: int = 0):
    segments = await db.get_all_segments(limit=limit, offset=offset)
    return {"segments": segments}
