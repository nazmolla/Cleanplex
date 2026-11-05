"""HTTP routes for stream-proxy enforcement (#184): on/off, status, and the Plex settings it needs."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import cleanplex.plex_client as plex_mod
from ... import database as db
from ... import events
from ...logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/enforcement", tags=["enforcement"])


class EnforcementUpdate(BaseModel):
    enabled: bool | None = None
    mute_mode: str | None = Field(default=None, pattern="^(cut|leave)$")
    backstop: bool | None = None


class PlexSettingsUpdate(BaseModel):
    relay: bool | None = None
    dlna: bool | None = None
    transcode_limit: int | None = Field(default=None, ge=0, le=50)


@router.get("")
async def get_enforcement():
    """Return enforcement settings, recent proxied streams and the related Plex settings."""
    plex = {}
    try:
        client = plex_mod.get_client()
        raw = await client.get_prefs(list(client.ENFORCEMENT_PREFS.values()))
        plex = {name: raw.get(key) for name, key in client.ENFORCEMENT_PREFS.items()}
    except Exception as exc:  # the page still shows Cleanplex's own settings without Plex
        logger.debug("Could not read Plex preferences: %s", exc)
    return {
        "enabled": await db.get_setting("enforcement_enabled", "0") == "1",
        "mute_mode": await db.get_setting("enforcement_mute_mode", "leave"),
        "backstop": await db.get_setting("enforcement_backstop", "1") == "1",
        "proxied_last_day": await db.count_recent_proxy_sessions(24),
        "plex": plex,
        # Shown as advice in the UI; relayed streams never reach the front door.
        "recommendations": {"relay": False, "dlna": False},
    }


@router.post("")
async def update_enforcement(body: EnforcementUpdate):
    """Update enforcement settings. `enabled` is set by the front door installer after the cutover."""
    if body.enabled is not None:
        await db.set_setting("enforcement_enabled", "1" if body.enabled else "0")
    if body.mute_mode is not None:
        await db.set_setting("enforcement_mute_mode", body.mute_mode)
    if body.backstop is not None:
        await db.set_setting("enforcement_backstop", "1" if body.backstop else "0")
    events.publish("settings_changed")
    return await get_enforcement()


@router.post("/plex-settings")
async def update_plex_settings(body: PlexSettingsUpdate):
    """Apply the Plex server settings the front door relies on (Relay, DLNA, transcode limit)."""
    try:
        client = plex_mod.get_client()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Plex not configured")
    values = {}
    if body.relay is not None:
        values["RelayEnabled"] = "1" if body.relay else "0"
    if body.dlna is not None:
        values["DlnaEnabled"] = "1" if body.dlna else "0"
    if body.transcode_limit is not None:
        values["TranscodeCountLimit"] = str(body.transcode_limit)
    if not values:
        raise HTTPException(status_code=422, detail="Nothing to change")
    try:
        await client.set_prefs(values)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Plex rejected the change: {exc}")
    logger.info("Applied Plex settings for the front door: %s", values)
    return await get_enforcement()
