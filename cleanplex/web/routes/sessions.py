from fastapi import APIRouter, HTTPException

from ...logger import get_logger
import cleanplex.plex_client as plex_mod
from ...watcher import skip_events
from ... import database as db
from ...scanner import get_queue_size, get_current_scan, is_paused

logger = get_logger(__name__)
router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("")
async def get_sessions():
    try:
        client = plex_mod.get_client()
    except RuntimeError:
        return {"sessions": [], "error": "Plex not configured"}

    sessions = await client.get_active_sessions()
    result = []
    for s in sessions:
        user_filter = await db.get_user_filter(s.user)
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
            "thumb_url": client.thumb_url(s.thumb) if s.thumb else "",
        })
    return {"sessions": result}


@router.get("/events")
async def get_skip_events():
    return {"events": list(skip_events)}


@router.get("/scanner-status")
async def scanner_status():
    current_guid = get_current_scan()
    current_title = None
    current_progress = 0.0
    if current_guid:
        job = await db.get_scan_job_by_guid(current_guid)
        if job:
            current_title = job["title"]
            current_progress = job["progress"]
    return {
        "queue_size": get_queue_size(),
        "current_scan": current_guid,
        "current_title": current_title,
        "current_progress": current_progress,
        "paused": is_paused(),
    }
