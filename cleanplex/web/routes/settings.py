from fastapi import APIRouter
from pydantic import BaseModel

from ...logger import get_logger
from ... import database as db
import cleanplex.plex_client as plex_mod
from ... import scanner as scan_mod

logger = get_logger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])

DETECTOR_LABELS = [
    # EXPOSED categories
    "FEMALE_GENITALIA_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "MALE_BREAST_EXPOSED",
    "ANUS_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "FEET_EXPOSED",
    "BELLY_EXPOSED",
    "ARMPITS_EXPOSED",
    # COVERED categories
    "FEMALE_GENITALIA_COVERED",
    "FEMALE_BREAST_COVERED",
    "MALE_BREAST_COVERED",
    "ANUS_COVERED",
    "BUTTOCKS_COVERED",
    "FEET_COVERED",
    "BELLY_COVERED",
    "ARMPITS_COVERED",
    # FACE categories
    "FACE_FEMALE",
    "FACE_MALE",
]


class SettingsPayload(BaseModel):
    plex_url: str | None = None
    plex_token: str | None = None
    poll_interval: str | None = None
    confidence_threshold: str | None = None
    skip_buffer_ms: str | None = None
    scan_step_ms: str | None = None
    scan_workers: str | None = None
    segment_gap_ms: str | None = None
    segment_min_hits: str | None = None
    scan_window_start: str | None = None
    scan_window_end: str | None = None
    log_level: str | None = None
    excluded_library_ids: str | None = None
    scan_ratings: str | None = None
    scan_labels: str | None = None


@router.get("")
async def get_settings():
    return await db.get_all_settings()


@router.put("")
async def update_settings(payload: SettingsPayload):
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    
    # Check if scan_workers is being changed
    scan_workers_changed = False
    if "scan_workers" in data:
        current_workers = await db.get_setting("scan_workers", "2")
        if data["scan_workers"] != current_workers:
            scan_workers_changed = True
            logger.info("Scan workers changing from %s to %s", current_workers, data["scan_workers"])
    
    await db.update_settings(data)
    
    # Reinitialise client if connection details changed
    if "plex_url" in data or "plex_token" in data:
        settings = await db.get_all_settings()
        url = settings.get("plex_url", "")
        token = settings.get("plex_token", "")
        if url and token:
            plex_mod.init_client(url, token)
    
    # Restart scanner pool if worker count changed
    if scan_workers_changed:
        await scan_mod.request_scanner_restart()
    
    return {"ok": True}


@router.post("/test-connection")
async def test_connection():
    settings = await db.get_all_settings()
    url = settings.get("plex_url", "")
    token = settings.get("plex_token", "")
    if not url or not token:
        return {"ok": False, "message": "Plex URL and token are required"}
    client = plex_mod.PlexClient(url, token)
    ok, message = await client.test_connection()
    return {"ok": ok, "message": message}


@router.get("/detector-labels")
async def get_detector_labels():
    return {"labels": DETECTOR_LABELS}
