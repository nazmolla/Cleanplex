"""HTTP surface for Hayaa: linking (#221), sync, settings and the review list (#222)."""

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ... import database as db
from ... import hayaa, hayaa_sync

router = APIRouter(prefix="/api/hayaa", tags=["hayaa"])


class HayaaUrls(BaseModel):
    api_url: str
    auth_url: str


def _http_error(exc: hayaa.HayaaError) -> HTTPException:
    if isinstance(exc, hayaa.HayaaBusy):
        return HTTPException(503, {"error": str(exc), "retry_after": exc.retry_after})
    return HTTPException(400 if not isinstance(exc, hayaa.NotLinked) else 409, str(exc))


@router.get("/status")
async def get_status():
    """Linked state, service URLs and any sign-in in progress; never secrets."""
    return await hayaa.status()


@router.put("/settings")
async def put_settings(payload: HayaaUrls):
    """Save the Hayaa service and sign-in URLs (https, or http to this machine for a dev server)."""
    urls = {"hayaa_api_url": payload.api_url, "hayaa_auth_url": payload.auth_url}
    for key, url in urls.items():
        try:
            urls[key] = hayaa._check_url(url, "service" if key == "hayaa_api_url" else "sign-in") if url.strip() else ""
        except hayaa.HayaaError as exc:
            raise HTTPException(422, str(exc)) from None
    for key, url in urls.items():
        await db.set_setting(key, url)
    return await hayaa.status()


@router.post("/link")
async def post_link():
    """Start signing in: returns the code and the link the user opens on another device."""
    try:
        return await hayaa.start_link()
    except hayaa.HayaaError as exc:
        raise _http_error(exc) from None


@router.post("/unlink")
async def post_unlink():
    """Revoke this instance on Hayaa and forget its sign-in; the outbox and key are kept."""
    await hayaa.unlink()
    return await hayaa.status()


class SyncSettings(BaseModel):
    auto_sync: bool | None = None
    interval_h: int | None = Field(default=None, ge=1, le=168)
    preset: str | None = None


class ReviewVerdict(BaseModel):
    title_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim_id: str = Field(min_length=1, max_length=64)
    keep: bool


_sync_task: asyncio.Task | None = None


async def _run_manual_sync(force: bool = False) -> None:
    try:
        await hayaa_sync.run_sync(manual=True, force=force)
    except hayaa.HayaaError:
        pass  # recorded in hayaa_last_result, which the status shows


@router.get("/sync")
async def get_sync():
    """Sync settings, quota use today, the last run and what waits for review or a re-check."""
    return await hayaa_sync.sync_status()


@router.post("/sync")
async def post_sync(force: bool = False):
    """Start one manual sync in the background; poll GET /api/hayaa/sync for the outcome.
    `?force=true` sends and pulls every title, not only what changed."""
    global _sync_task
    if not hayaa.is_linked():
        raise HTTPException(409, "Sign in to Hayaa first")
    if hayaa_sync.running():
        raise HTTPException(409, "A sync is already running")
    _sync_task = asyncio.create_task(_run_manual_sync(force))
    await asyncio.sleep(0)  # let it take the lock, so the status below says running
    return await hayaa_sync.sync_status()


@router.put("/sync")
async def put_sync(payload: SyncSettings):
    """Change automatic sync, its interval in hours, or the apply preset."""
    if payload.preset is not None:
        if payload.preset not in hayaa_sync.PRESETS:
            raise HTTPException(422, f"preset must be one of {', '.join(hayaa_sync.PRESETS)}")
        await db.set_setting("hayaa_preset", payload.preset)
        await db.set_setting("hayaa_preset_auto_switched", "0")
    if payload.auto_sync is not None:
        await db.set_setting("hayaa_auto_sync", "1" if payload.auto_sync else "0")
    if payload.interval_h is not None:
        await db.set_setting("hayaa_sync_interval_h", str(payload.interval_h))
    if payload.preset is not None:
        await hayaa_sync.apply_titles(sorted({c["plex_guid"] for c in await db.get_hayaa_claims()}))
    return await hayaa_sync.sync_status()


@router.get("/review")
async def get_review():
    """Pulled claims held for review, and segments the community marked false positive."""
    return {
        "held": await db.get_hayaa_review(),
        "community_false_positives": await db.get_community_false_positives(),
    }


@router.post("/review")
async def post_review(payload: ReviewVerdict):
    """Keep (confirm, applied and voted for) or dismiss (never applied) a held claim."""
    claims = await db.get_hayaa_claims()
    if not any(c["title_key"] == payload.title_key and c["claim_id"] == payload.claim_id for c in claims):
        raise HTTPException(404, "Unknown claim")
    return await hayaa_sync.review(payload.title_key, payload.claim_id, payload.keep)
