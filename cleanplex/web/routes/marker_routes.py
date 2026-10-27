"""HTTP routes for reading and editing Plex native intro/credits markers."""

from __future__ import annotations

import asyncio
import mimetypes
import os
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from fastapi.responses import FileResponse, Response

from ...logger import get_logger
import cleanplex.plex_client as plex_mod
from ... import database as db
from ... import events

logger = get_logger(__name__)
router = APIRouter(prefix="/api/markers", tags=["markers"])

_SYNC_ALL_CONCURRENCY = 5  # max simultaneous Plex marker fetches during bulk sync


def _plex_image_proxy_url(path: str) -> str:
    if not path:
        return ""
    return f"/api/plex-image?path={quote(path, safe='')}"


class MarkerUpdateRequest(BaseModel):
    start_ms: int
    end_ms: int


class MarkerSyncRequest(BaseModel):
    plex_guid: str


class MarkerCreateRequest(BaseModel):
    plex_guid: str
    marker_type: str  # "intro" or "credits"
    start_ms: int
    end_ms: int


# ── Libraries / titles ────────────────────────────────────────────────────────

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
async def get_library_titles(library_id: str):
    """Return cached titles for a library joined with current marker counts.

    Reads from DB — instant response. Use POST /refresh to pull a fresh snapshot from Plex.
    Returns cached=False when the cache is empty so the UI can prompt for a refresh.
    """
    rows = await db.get_plex_title_cache(library_id)
    if not rows:
        return {"titles": [], "cached": False}

    result = []
    for row in rows:
        rk = row["rating_key"]
        show_rating_key = row["show_rating_key"] or ""
        if row["media_type"] == "episode":
            thumb_url = _plex_image_proxy_url(f"/library/metadata/{rk}/thumb")
            poster_url = (
                _plex_image_proxy_url(f"/library/metadata/{show_rating_key}/thumb")
                if show_rating_key else ""
            )
        else:
            thumb_url = _plex_image_proxy_url(f"/library/metadata/{rk}/thumb")
            poster_url = thumb_url

        result.append({
            "plex_guid": row["plex_guid"],
            "title": row["title"],
            "rating_key": rk,
            "media_type": row["media_type"],
            "show_guid": row["show_guid"] or "",
            "show_title": row["show_title"] or "",
            "show_rating_key": show_rating_key,
            "content_rating": row["content_rating"] or "",
            "year": row["year"],
            "thumb_url": thumb_url,
            "poster_url": poster_url,
            "marker_count": row["marker_count"],
        })

    return {"titles": result, "cached": True}


@router.post("/libraries/{library_id}/refresh")
async def refresh_library_titles(library_id: str):
    """Fetch all titles from Plex and rebuild the DB cache for this library.

    Slow — hits Plex API. Run on demand only.
    """
    try:
        client = plex_mod.get_client()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Plex not configured")

    items = await client.get_library_items(library_id)
    if not items:
        raise HTTPException(status_code=502, detail="Plex returned no items — check library ID and connection")

    cache_rows = []
    for item in items:
        show_title = ""
        show_rating_key = item.show_rating_key or ""
        if item.media_type == "episode":
            # grandparentTitle is populated by bulk episode fetch — no extra API call needed.
            show_title = item.title.split(" – ")[0].strip()
        cache_rows.append({
            "rating_key": item.rating_key,
            "plex_guid": item.plex_guid,
            "title": item.title,
            "media_type": item.media_type,
            "show_guid": item.show_guid or "",
            "show_title": show_title,
            "show_rating_key": show_rating_key,
            "content_rating": item.content_rating or "",
            "year": item.year,
        })

    await db.upsert_plex_title_cache(library_id, cache_rows)
    logger.info("Refreshed title cache for library %s — %d items", library_id, len(cache_rows))
    events.publish("markers_changed")
    return {"ok": True, "count": len(cache_rows)}


# ── Marker sync / fetch ───────────────────────────────────────────────────────

@router.post("/titles/{rating_key}/sync")
async def sync_markers_for_title(rating_key: str, body: MarkerSyncRequest):
    """Pull fresh markers from Plex for a title and upsert into local DB.

    rating_key (always unique per Plex item) is the URL param.
    plex_guid is passed in the body for storage as a cross-reference.
    """
    try:
        client = plex_mod.get_client()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Plex not configured")

    try:
        markers = await client.get_markers(rating_key)
        await db.upsert_plex_markers(body.plex_guid, rating_key, markers)
        logger.debug("Synced rating_key=%s — %d marker(s)", rating_key, len(markers))
        events.publish("markers_changed")
        return {"ok": True, "markers": markers, "count": len(markers)}
    except Exception as exc:
        logger.error("Marker sync failed rating_key=%s guid=%s — %s", rating_key, body.plex_guid, exc)
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/titles/{rating_key}")
async def get_markers_for_title(rating_key: str):
    """Return stored markers for a title from local DB, looked up by rating_key."""
    markers = await db.get_plex_markers_for_rating_key(rating_key)
    return {"markers": markers}


@router.post("/titles/{rating_key}/create")
async def create_marker(rating_key: str, body: MarkerCreateRequest):
    """Create a new marker in local DB. Attempts to write to Plex but succeeds even if Plex rejects it.

    Markers created here have plex_marker_id=NULL (locally managed).
    """
    if body.marker_type not in ("intro", "credits"):
        raise HTTPException(status_code=422, detail="marker_type must be 'intro' or 'credits'")
    if body.start_ms >= body.end_ms:
        raise HTTPException(status_code=422, detail="start_ms must be less than end_ms")

    marker = await db.insert_plex_marker(body.plex_guid, rating_key, body.marker_type, body.start_ms, body.end_ms)

    # Best-effort Plex write — requires Plex Pass. Failure is non-fatal.
    plex_error: str | None = None
    try:
        client = plex_mod.get_client()
        await client.create_marker(rating_key, body.marker_type, body.start_ms, body.end_ms)
    except Exception as exc:
        plex_error = str(exc)
        logger.warning("Could not write new marker to Plex (rating_key=%s): %s", rating_key, exc)

    events.publish("markers_changed")
    return {"ok": True, "marker": marker, "plex_error": plex_error}


# ── Marker update ─────────────────────────────────────────────────────────────

@router.delete("/{marker_id}")
async def delete_marker(marker_id: int):
    """Delete a stored marker from the local DB (does not modify Plex)."""
    deleted = await db.delete_plex_marker(marker_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Marker not found")
    events.publish("markers_changed")
    return {"ok": True}


@router.patch("/{marker_id}")
async def update_marker(marker_id: int, body: MarkerUpdateRequest):
    """Update marker start/end timestamps in DB and write back to Plex.

    Returns 403 if Plex rejects due to missing Plex Pass subscription.
    """
    marker = await db.get_plex_marker(marker_id)
    if not marker:
        raise HTTPException(status_code=404, detail="Marker not found")

    if body.start_ms >= body.end_ms:
        raise HTTPException(status_code=422, detail="start_ms must be less than end_ms")

    try:
        client = plex_mod.get_client()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Plex not configured")

    try:
        await client.update_marker(
            rating_key=marker["rating_key"],
            plex_marker_id=marker["plex_marker_id"],
            start_ms=body.start_ms,
            end_ms=body.end_ms,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    await db.update_plex_marker_timestamps(marker_id, body.start_ms, body.end_ms)

    updated = await db.get_plex_marker(marker_id)
    events.publish("markers_changed")
    return {"ok": True, "marker": updated}


# ── Video stream ──────────────────────────────────────────────────────────────

@router.get("/{marker_id}/vlc")
async def vlc_marker(marker_id: int, request: Request):
    """Return an M3U playlist that opens the marker source file in VLC."""
    marker = await db.get_plex_marker(marker_id)
    if not marker:
        raise HTTPException(status_code=404, detail="Marker not found")
    base = str(request.base_url).rstrip("/")
    stream_url = f"{base}/api/markers/{marker_id}/stream"
    title = marker.get("plex_guid") or "Marker"
    m3u = f"#EXTM3U\n#EXTINF:-1,{title}\n{stream_url}\n"
    return Response(content=m3u, media_type="audio/x-mpegurl",
                    headers={"Content-Disposition": f'attachment; filename="marker_{marker_id}.m3u"'})


@router.get("/{marker_id}/stream")
async def stream_marker_source(marker_id: int):
    """Stream the full source media file for a marker with HTTP range support.

    Streams the unclipped file so the browser <video> element can seek freely
    across the full duration — essential for the timeline drag editor.
    """
    marker = await db.get_plex_marker(marker_id)
    if not marker:
        raise HTTPException(status_code=404, detail="Marker not found")

    job = await db.get_scan_job_by_guid(marker["plex_guid"])
    if not job or not job.get("file_path"):
        raise HTTPException(status_code=404, detail="Source file not found — title must be synced to the scan library first")

    file_path = job["file_path"]
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="Source media file does not exist on disk")

    media_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    # FileResponse handles HTTP 206 range requests automatically — required for
    # browser <video> seek to work without loading the full file first.
    return FileResponse(path=file_path, media_type=media_type)
