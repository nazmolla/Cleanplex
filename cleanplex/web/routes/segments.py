import asyncio
import json
import mimetypes
import os
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from ...logger import get_logger
import cleanplex.plex_client as plex_mod
from ... import database as db

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["segments"])

# Pending debounce tasks keyed by plex_guid — cancelled and replaced on each delete.
# This ensures bulk deletes trigger at most one Plex metadata write per title.
_pending_summary_tasks: dict[str, asyncio.Task] = {}


async def _do_refresh_summary(plex_guid: str) -> None:
    """Perform the actual Plex summary metadata update after a short debounce delay."""
    await asyncio.sleep(2)  # coalesce rapid sequential deletes
    _pending_summary_tasks.pop(plex_guid, None)

    try:
        job = await db.get_scan_job_by_guid(plex_guid)
        if not job:
            return

        rating_key = str(job.get("rating_key") or "")
        if not rating_key:
            return

        # COUNT query — avoids loading all segment rows just to get the total.
        segment_count = await db.count_segments_for_guid(plex_guid)
        status = "Scanned" if job.get("status") == "done" else "Pending"

        client = plex_mod.get_client()
        await client.update_cleanplex_summary(
            rating_key=rating_key,
            status=status,
            segment_count=segment_count,
        )
    except Exception as exc:
        logger.debug("Could not refresh Plex summary for guid=%s: %s", plex_guid, exc)


def _refresh_cleanplex_summary_for_guid(plex_guid: str) -> None:
    """Schedule a debounced Plex summary refresh.

    Any pending refresh for the same guid is cancelled before scheduling a new
    one, so bulk deletes of N segments produce at most one metadata write.
    """
    existing = _pending_summary_tasks.pop(plex_guid, None)
    if existing and not existing.done():
        existing.cancel()
    task = asyncio.create_task(_do_refresh_summary(plex_guid))
    _pending_summary_tasks[plex_guid] = task


def _plex_image_proxy_url(path: str) -> str:
    if not path:
        return ""
    return f"/api/plex-image?path={quote(path, safe='')}"


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
        file_items = [i for i in items if i.file_path]
        existing_guids = await db.get_existing_guids([i.plex_guid for i in file_items])

        # Refresh mutable Plex metadata for all existing titles in one transaction
        # so that manual rating changes in Plex are reflected after sync.
        await db.refresh_scan_job_metadata_batch([
            (i.plex_guid, i.title, i.file_path, i.rating_key, i.content_rating, i.year)
            for i in file_items if i.plex_guid in existing_guids
        ])

        # Remove DB entries for titles Plex no longer reports in this library
        # (e.g. deleted files or removed duplicates).
        plex_guids = [i.plex_guid for i in file_items]
        removed = await db.delete_scan_jobs_not_in(library_id, plex_guids)
        if removed:
            logger.info(f"Library {library_id} sync: removed {removed} stale titles")

        added = 0
        for item in file_items:
            if item.plex_guid in existing_guids:
                continue
            if scan_ratings:
                # Filter strictly: "" = unrated, only included when the
                # Unrated checkbox is ticked (saves "" in scan_ratings).
                if (item.content_rating or "") not in scan_ratings:
                    continue
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
        return {"ok": True, "synced": len(items), "new": added, "removed": removed}
    except RuntimeError as e:
        logger.error(f"Plex client error during library sync: {e}")
        return {"ok": False, "error": str(e)}


@router.get("/libraries/{library_id}/titles")
async def get_titles_in_library(library_id: str):
    """Return all scan jobs (titles) for a given library, with Plex poster URLs."""
    jobs = await db.get_scan_jobs_by_library(library_id)
    seg_counts = await db.get_segment_counts_for_library(library_id)

    # Apply scan_ratings filter to the library view so it matches what the
    # scanner will actually process.
    scan_ratings_raw = json.loads(await db.get_setting("scan_ratings", "[]"))
    scan_ratings: set[str] = set(scan_ratings_raw)
    if scan_ratings:
        # Filter strictly by the ratings the user configured.
        # "" (empty) = Plex left the title unrated; it only shows when the
        # "Unrated" checkbox is ticked, which stores "" in scan_ratings.
        jobs = [j for j in jobs if (j.get("content_rating") or "") in scan_ratings]
    try:
        client = plex_mod.get_client()
    except RuntimeError:
        client = None

    result = []
    # Cache resolved show metadata by show name to avoid repeated Plex API calls.
    # Value: (show_guid, show_title, show_poster_url, show_rating_key, season_rating_key)
    show_meta_by_name: dict[str, tuple[str, str, str, str, str]] = {}
    for job in jobs:
        thumb_url = ""
        poster_url = ""
        show_guid = ""
        show_title = ""
        show_rating_key = ""
        season_rating_key = ""
        if client and job.get("rating_key"):
            rating_key = job["rating_key"]
            thumb_url = _plex_image_proxy_url(f"/library/metadata/{rating_key}/thumb")
            if job.get("media_type") == "episode":
                # Resolve true show-level poster path from episode metadata.
                # Example resolved path from Plex: /library/metadata/<showKey>/thumb/<version>
                # which matches how Plex itself loads show posters.
                parsed_show_name = (job.get("title", "").split(" \u2013 ")[0] or "").strip()
                if parsed_show_name and parsed_show_name in show_meta_by_name:
                    show_guid, show_title, poster_url, show_rating_key, season_rating_key = show_meta_by_name[parsed_show_name]
                else:
                    resolved_show_guid, resolved_show_title, show_thumb_path, resolved_show_rk, resolved_season_rk = await client.get_episode_show_art(rating_key)
                    show_guid = resolved_show_guid
                    show_title = resolved_show_title or parsed_show_name
                    poster_url = _plex_image_proxy_url(show_thumb_path) if show_thumb_path else ""
                    show_rating_key = resolved_show_rk
                    season_rating_key = resolved_season_rk
                    if parsed_show_name:
                        show_meta_by_name[parsed_show_name] = (show_guid, show_title, poster_url, show_rating_key, season_rating_key)
            else:
                poster_url = thumb_url
        result.append({
            "plex_guid": job["plex_guid"],
            "rating_key": job.get("rating_key", ""),
            "title": job["title"],
            "status": job["status"],
            "progress": job["progress"],
            "finished_at": job.get("finished_at"),
            "thumb_url": thumb_url,
            "poster_url": poster_url,
            "show_guid": show_guid,
            "show_title": show_title,
            "show_rating_key": show_rating_key,
            "season_rating_key": season_rating_key,
            "segment_count": seg_counts.get(job["plex_guid"], 0),
            "content_rating": job.get("content_rating", ""),
            "media_type": job.get("media_type", "movie"),
            "year": job.get("year"),
            "ignored": bool(job.get("ignored", 0)),
        })
    return {"titles": result}


@router.get("/plex-image")
async def get_plex_image(path: str):
    """Proxy Plex images through Cleanplex so artwork loads from remote clients."""
    if not path:
        raise HTTPException(status_code=400, detail="Missing image path")

    try:
        client = plex_mod.get_client()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Plex not configured")

    content, content_type = await client.fetch_image(path)
    if not content:
        raise HTTPException(status_code=404, detail="Image not available")

    return Response(content=content, media_type=content_type or "image/jpeg")


@router.get("/titles/{plex_guid:path}/segments")
async def get_segments_for_title(plex_guid: str):
    """Return all segments for a specific title with all detected labels."""
    segments = await db.get_segments_for_guid(plex_guid)
    scan_labels_raw = json.loads(await db.get_setting("scan_labels", "[]"))
    enabled_labels = set(scan_labels_raw) if isinstance(scan_labels_raw, list) else set()
    result = []
    for seg in segments:
        labels = seg.get("labels", "") or ""
        if enabled_labels and labels:
            filtered = [l.strip() for l in labels.split(",") if l.strip() in enabled_labels]
            labels = ",".join(filtered)
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
            "labels": labels,
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
    seg = await db.get_segment_by_id(segment_id)
    if not seg:
        raise HTTPException(status_code=404, detail="Segment not found")

    deleted = await db.delete_segment(segment_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Segment not found")

    _refresh_cleanplex_summary_for_guid(seg["plex_guid"])

    return {"ok": True}


@router.delete("/titles/{plex_guid:path}/segments")
async def delete_all_segments_for_title(plex_guid: str):
    """Delete all segments for a specific title."""
    deleted = await db.delete_segments_for_guid(plex_guid)
    _refresh_cleanplex_summary_for_guid(plex_guid)
    return {"ok": True, "deleted": deleted}


@router.get("/segments")
async def get_all_segments(limit: int = 100, offset: int = 0):
    segments = await db.get_all_segments(limit=limit, offset=offset)
    return {"segments": segments}
