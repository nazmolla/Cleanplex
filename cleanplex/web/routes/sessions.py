import json
from urllib.parse import quote

from fastapi import APIRouter, HTTPException

from ...logger import get_logger
import cleanplex.plex_client as plex_mod
from ...watcher import skip_events
from ...filter_engine import _is_filtered
from ... import database as db
from ...scanner import get_current_scans, get_worker_pool_size, is_paused, rating_allowed

logger = get_logger(__name__)
router = APIRouter(prefix="/api/sessions", tags=["sessions"])


async def _filtering_state(session, enabled: bool, prefs_cache: dict[str, dict]) -> str:
    """Say whether this viewer's stream is being cut, and if not, why (#192).

    A stream is cut only when all three hold: the viewer's filter is on, the title is
    scanned, and it has a segment that fires at the viewer's levels (active). Otherwise:
    off (filter switched off); clear (scanned, nothing to cut); ignored / library_excluded /
    rating_skipped (not scanned because of those settings); not_scanned (waiting for a scan).
    """
    if not enabled:
        return "off"
    # Same lookup order as the filter engine: session GUIDs can differ from scan GUIDs.
    job = await db.get_scan_job_by_guid(session.plex_guid)
    if job is None and session.rating_key:
        job = await db.get_scan_job_by_rating_key(session.rating_key)
    if job is None:
        return "not_scanned"
    if job["status"] != "done":
        if job.get("ignored"):
            return "ignored"
        if job.get("library_id") in set(json.loads(await db.get_setting("excluded_library_ids", "[]"))):
            return "library_excluded"
        if not rating_allowed(job.get("content_rating"), json.loads(await db.get_setting("scan_ratings", "[]"))):
            return "rating_skipped"
        return "not_scanned"
    segments = await db.get_segments_for_guid(session.plex_guid)
    if not segments and session.rating_key:
        segments = await db.get_segments_by_rating_key(session.rating_key)
    if session.user not in prefs_cache:
        prefs_cache[session.user] = await db.get_user_category_prefs(session.user)
    prefs = prefs_cache[session.user]
    return "active" if any(_is_filtered(seg, prefs) for seg in segments) else "clear"


@router.get("")
async def get_sessions():
    try:
        client = plex_mod.get_client()
    except RuntimeError:
        return {"sessions": [], "error": "Plex not configured"}

    sessions = await client.get_active_sessions()
    # Load all user filters in one query and map by username to avoid N+1 DB calls.
    all_filters = {f["plex_username"]: f for f in await db.get_all_user_filters()}
    result = []
    prefs_cache: dict[str, dict] = {}
    for s in sessions:
        user_filter = all_filters.get(s.user)
        filtering_enabled = user_filter is None or bool(user_filter["enabled"])
        result.append({
            "session_key": s.session_key,
            "user": s.user,
            "title": s.full_title,
            "media_type": s.media_type,
            "position_ms": s.position_ms,
            "duration_ms": s.duration_ms,
            "client": s.client_title,
            "is_controllable": s.is_controllable,
            "filtering_enabled": filtering_enabled,
            "filtering_state": await _filtering_state(s, filtering_enabled, prefs_cache),
            # Same-origin image proxy: the direct Plex URL carries the server token (#183).
            "thumb_url": f"/api/plex-image?path={quote(s.thumb, safe='')}" if s.thumb else "",
        })
    return {"sessions": result}


@router.get("/events")
async def get_skip_events():
    return {"events": list(skip_events)}


@router.get("/scanner-status")
async def scanner_status():
    current_guids = await get_current_scans()
    current_guid = current_guids[0] if current_guids else None
    current_title = None
    current_progress = 0.0
    active_scans: list[dict] = []

    # Fetch all active scan jobs in a single IN query rather than one query per guid.
    jobs_by_guid = await db.get_scan_jobs_by_guids(current_guids)
    for guid in current_guids:
        job = jobs_by_guid.get(guid)
        if not job:
            continue
        active_scans.append({
            "guid": guid,
            "title": job.get("title") or guid,
            "progress": float(job.get("progress") or 0.0),
            "status": job.get("status") or "scanning",
        })

    if current_guid:
        job = jobs_by_guid.get(current_guid)
        if job:
            current_title = job["title"]
            current_progress = job["progress"]

    configured_workers = max(1, int(await db.get_setting("scan_workers", "2")))
    effective_workers = max(1, int(get_worker_pool_size()))
    active_workers = len(active_scans)

    # The queue in execution order (force first), straight from the database.
    queue_items = [
        {
            "position": pos + 1,
            "guid": job["plex_guid"],
            "title": job["title"] or job["plex_guid"],
            "media_type": job.get("media_type") or "movie",
            "content_rating": job.get("content_rating") or "",
            "force": bool(job.get("force_scan")),
        }
        for pos, job in enumerate(await db.get_queued_scan_jobs())
    ]

    return {
        "queue_size": len(queue_items),
        "current_scan": current_guid,
        "current_title": current_title,
        "current_progress": current_progress,
        "current_scans": current_guids,
        "active_scans": active_scans,
        "workers_configured": effective_workers,
        "workers_target": configured_workers,
        "workers_active": active_workers,
        "workers_idle": max(0, effective_workers - active_workers),
        "paused": is_paused(),
        "queue_items": queue_items,
    }


@router.post("/{session_key}/skip")
async def skip_session_title(session_key: str):
    """Skip active playback to the end of the current (or next) detected segment."""
    try:
        client = plex_mod.get_client()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Plex not configured")

    sessions = await client.get_active_sessions()
    session = next((s for s in sessions if s.session_key == session_key), None)
    if session is None:
        raise HTTPException(status_code=404, detail="Active session not found")

    if not session.is_controllable:
        raise HTTPException(status_code=409, detail="Session is not remotely controllable")

    segments = await db.get_segments_for_guid(session.plex_guid)
    if not segments and session.rating_key:
        segments = await db.get_segments_by_rating_key(session.rating_key)

    if not segments:
        raise HTTPException(status_code=404, detail="No detected segments found for this title")

    # Expand segment boundaries by 5 seconds before and after
    for seg in segments:
        seg["start_ms"] = max(0, int(seg["start_ms"]) - 5000)
        seg["end_ms"] = int(seg["end_ms"]) + 5000

    pos = int(session.position_ms)

    # Prefer the segment currently playing; otherwise choose the next segment ahead.
    current = next((seg for seg in segments if int(seg["start_ms"]) <= pos <= int(seg["end_ms"])), None)
    target_seg = current or next((seg for seg in segments if int(seg["start_ms"]) > pos), None)
    if target_seg is None:
        raise HTTPException(status_code=409, detail="No remaining segments ahead of current position")

    skip_buffer_ms = int(await db.get_setting("skip_buffer_ms", "3000"))
    # Seek to the expanded segment start
    seek_to_ms = int(target_seg["start_ms"])

    ok = await client.seek(
        session.client_identifier,
        seek_to_ms,
        session.client_address,
        session.client_port,
    )
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to seek Plex client")

    return {
        "ok": True,
        "session_key": session.session_key,
        "title": session.full_title,
        "seek_to_ms": seek_to_ms,
        "segment_start_ms": int(target_seg["start_ms"]),
        "segment_end_ms": int(target_seg["end_ms"]),
        "client": session.client_title,
        "user": session.user,
    }
