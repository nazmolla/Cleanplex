import json
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator

from ...logger import get_logger
from ... import database as db
from ... import events
from ...importers._common import DETECTOR_LABELS as _DETECTOR_LABELS
import cleanplex.plex_client as plex_mod
from ... import scanner as scan_mod
from .segments import _invalidate_scan_labels_cache

logger = get_logger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])

DETECTOR_LABELS = list(_DETECTOR_LABELS)


class SettingsPayload(BaseModel):
    plex_url: str | None = None
    plex_token: str | None = None
    poll_interval: str | None = None
    confidence_threshold: str | None = None
    skip_buffer_ms: str | None = None
    pre_buffer_ms: str | None = None
    post_buffer_ms: str | None = None
    scan_step_ms: str | None = None
    scan_workers: str | None = None
    nudenet_model: str | None = None
    nudenet_model_path: str | None = None
    segment_gap_ms: str | None = None
    segment_min_hits: str | None = None
    scan_window_start: str | None = None
    scan_window_end: str | None = None
    log_level: str | None = None
    excluded_library_ids: str | None = None
    scan_ratings: str | None = None
    scan_labels: str | None = None
    profanity_wordlist: str | None = None
    # Validated here because a bad value would otherwise only surface as a scan
    # failure in Config.load(), long after the user pressed Save.
    siglip_enabled: Literal["0", "1"] | None = None
    siglip_threshold: str | None = Field(None, pattern=r"^(0(\.\d+)?|1(\.0+)?)$")
    siglip_min_hits: str | None = Field(None, pattern=r"^[1-9]$")
    ignore_credits: Literal["0", "1"] | None = None
    verifier_loss_pct: Literal["0", "5", "10", "20"] | None = None
    # 0 (off) or 100-5000 ms; finer is slower on every flagged segment.
    refine_step_ms: str | None = Field(None, pattern=r"^(0|[1-9]\d{2}|[1-4]\d{3}|5000)$")
    inference_threads: str | None = Field(None, pattern=r"^([0-9]|[1-5][0-9]|6[0-4])$")
    # JSON {LABEL: 0..1}; a label left out inherits the global threshold.
    label_thresholds: str | None = None
    siglip_label_thresholds: str | None = None
    hw_decode: Literal["0", "1"] | None = None
    hover_previews_enabled: Literal["0", "1"] | None = None
    hover_when_idle: Literal["0", "1"] | None = None
    hover_window_start: str | None = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    hover_window_end: str | None = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


    @field_validator("label_thresholds", "siglip_label_thresholds")
    @classmethod
    def _valid_threshold_map(cls, value: str | None) -> str | None:
        """Reject anything but a JSON object of known label -> number in 0..1."""
        if value is None:
            return value
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise ValueError("must be a JSON object") from exc
        if not isinstance(parsed, dict):
            raise ValueError("must be a JSON object")
        for label, threshold in parsed.items():
            if label not in DETECTOR_LABELS:
                raise ValueError(f"unknown label {label!r}")
            if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1:
                raise ValueError(f"threshold for {label} must be a number between 0 and 1")
        return value


class ValidateModelPathPayload(BaseModel):
    nudenet_model: str = "320n"
    nudenet_model_path: str = ""


@router.get("")
async def get_settings():
    """Return all settings; the Plex token is never sent, only whether one is stored (#155)."""
    settings = await db.get_all_settings()
    settings["plex_token_set"] = bool(settings.get("plex_token"))
    settings["plex_token"] = ""
    return settings


@router.get("/plex-server-id")
async def get_plex_server_id():
    """Return the Plex server machine identifier for constructing web deep links."""
    try:
        client = plex_mod.get_client()
        machine_id = await client.get_machine_identifier()
        return {"machine_identifier": machine_id}
    except RuntimeError:
        return {"machine_identifier": ""}
    except Exception as exc:
        logger.warning("Failed to get machine identifier: %s", exc)
        return {"machine_identifier": ""}


@router.put("")
async def update_settings(payload: SettingsPayload):
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    # GET never returns the token, so a blank value means "unchanged", not "clear it" (#155).
    if not data.get("plex_token"):
        data.pop("plex_token", None)

    # Check if scan_workers is being changed
    scan_workers_changed = False
    # A new thread cap only reaches inference sessions when the pool is rebuilt.
    for key, fallback in (("scan_workers", "2"), ("inference_threads", "0")):
        if key in data:
            current = await db.get_setting(key, fallback)
            if data[key] != current:
                scan_workers_changed = True
                logger.info("%s changing from %s to %s", key, current, data[key])
    
    await db.update_settings(data)

    # Invalidate scan_labels cache so the next segment expand reflects the new value.
    if "scan_labels" in data:
        _invalidate_scan_labels_cache()

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

    events.publish("settings_changed")
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


@router.post("/validate-model-path")
async def validate_model_path(payload: ValidateModelPathPayload):
    model_name = (payload.nudenet_model or "320n").strip().lower()

    # 320n is bundled with nudenet package; no download required.
    if not model_name.startswith("640"):
        return {
            "ok": True,
            "message": "320n uses bundled model; no custom file path required.",
        }

    try:
        # Scanner auto-downloads 640m model into app storage on first use.
        from ... import scanner as scanner_mod

        path = scanner_mod._ensure_local_640m_model()
        if not path:
            return {
                "ok": False,
                "message": "Could not download 640m model automatically. Check internet access and try again.",
            }
        return {
            "ok": True,
            "message": "640m model is ready in local app storage.",
        }
    except Exception as exc:
        logger.warning("NudeNet auto-download validation failed: %s", exc)
        return {
            "ok": False,
            "message": f"Model could not be prepared: {exc}",
        }
