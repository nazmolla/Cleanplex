import json
import mimetypes
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
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


@router.post("/libraries/{library_id}/sync")
async def sync_library(library_id: str):
    """Sync a library from Plex into DB. Returns count of new titles added."""
    excluded = set(json.loads(await db.get_setting("excluded_library_ids", "[]")))
    if library_id in excluded:
        return {"ok": False, "error": "Library is excluded from scanning"}
    try:
        client = plex_mod.get_client()
        items = await client.get_library_items(library_id)
        logger.info(f"Syncing library {library_id}: found {len(items)} items from Plex")
        
        scan_ratings = set(json.loads(await db.get_setting("scan_ratings", "[]")))
        added = 0
        for item in items:
            if item.file_path:
                if scan_ratings and item.content_rating not in scan_ratings:
                    continue
                existing = await db.get_scan_job_by_guid(item.plex_guid)
                if not existing:
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
                    )
                    added += 1
        
        logger.info(f"Library {library_id} synced: {added} new titles added")
        return {"ok": True, "synced": len(items), "new": added}
    except RuntimeError as e:
        logger.error(f"Plex client error during library sync: {e}")
        return {"ok": False, "error": str(e)}


@router.get("/libraries/{library_id}/titles")
async def get_titles_in_library(library_id: str):
    """Return all scan jobs (titles) for a given library, with Plex poster URLs."""
    jobs = await db.get_scan_jobs_by_library(library_id)
    seg_counts = await db.get_segment_counts_for_library(library_id)
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
            "segment_count": seg_counts.get(job["plex_guid"], 0),
            "content_rating": job.get("content_rating", ""),
            "media_type": job.get("media_type", "movie"),
            "year": job.get("year"),
            "ignored": bool(job.get("ignored", 0)),
        })
    return {"titles": result}


@router.get("/titles/{plex_guid:path}/segments")
async def get_segments_for_title(plex_guid: str):
    """Return all segments for a specific title with all detected labels."""
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
            "labels": seg.get("labels", ""),
        })
    return {"segments": result}


@router.post("/segments/{segment_id}/jump")
async def jump_to_segment(segment_id: int):
    """Seek an active, controllable Plex session for this title to the segment start."""
    seg = await db.get_segment_by_id(segment_id)
    if not seg:
        raise HTTPException(status_code=404, detail="Segment not found")

    job = await db.get_scan_job_by_guid(seg["plex_guid"])

    try:
        client = plex_mod.get_client()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Plex not configured")

    sessions = await client.get_active_sessions()

    target = None
    rating_key = str(job.get("rating_key", "")) if job else ""

    if rating_key:
        target = next(
            (s for s in sessions if s.is_controllable and str(s.rating_key) == rating_key),
            None,
        )

    if target is None:
        target = next(
            (s for s in sessions if s.is_controllable and s.plex_guid == seg["plex_guid"]),
            None,
        )

    if target is None:
        raise HTTPException(
            status_code=409,
            detail="No active controllable Plex playback found for this title. Start the title in Plex first.",
        )

    ok = await client.seek(
        target.client_identifier,
        int(seg["start_ms"]),
        target.client_address,
        target.client_port,
    )
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to seek Plex client")

    return {
        "ok": True,
        "segment_id": segment_id,
        "seek_to_ms": int(seg["start_ms"]),
        "client": target.client_title,
        "user": target.user,
    }


@router.get("/segments/{segment_id}/stream")
async def stream_segment_source(segment_id: int):
    """Stream the source media file for a segment so the web UI can preview it directly."""
    seg = await db.get_segment_by_id(segment_id)
    if not seg:
        raise HTTPException(status_code=404, detail="Segment not found")

    job = await db.get_scan_job_by_guid(seg["plex_guid"])
    if not job or not job.get("file_path"):
        raise HTTPException(status_code=404, detail="Source file not found for this segment")

    file_path = job["file_path"]
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="Source media file does not exist on disk")

    media_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    return FileResponse(path=file_path, media_type=media_type)


@router.delete("/segments/{segment_id}")
async def delete_segment(segment_id: int):
    deleted = await db.delete_segment(segment_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Segment not found")
    return {"ok": True}


@router.delete("/titles/{plex_guid:path}/segments")
async def delete_all_segments_for_title(plex_guid: str):
    """Delete all segments for a specific title."""
    deleted = await db.delete_segments_for_guid(plex_guid)
    return {"ok": True, "deleted": deleted}


@router.get("/segments")
async def get_all_segments(limit: int = 100, offset: int = 0):
    segments = await db.get_all_segments(limit=limit, offset=offset)
    return {"segments": segments}
