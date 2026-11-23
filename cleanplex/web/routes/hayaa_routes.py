"""HTTP surface for linking this instance to Hayaa (#221)."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ... import database as db
from ... import hayaa

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
