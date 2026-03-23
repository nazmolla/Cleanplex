from fastapi import APIRouter
from pydantic import BaseModel

from ...logger import get_logger
import cleanplex.plex_client as plex_mod
from ... import database as db

logger = get_logger(__name__)
router = APIRouter(prefix="/api/users", tags=["users"])


class UserFilterUpdate(BaseModel):
    enabled: bool


@router.get("")
async def get_users():
    """Return all Plex users merged with their filter settings."""
    # Get users from Plex if available
    plex_users: list[dict] = []
    try:
        client = plex_mod.get_client()
        users = await client.get_all_users()
        plex_users = [{"username": u.username, "thumb": u.thumb} for u in users]
    except RuntimeError:
        pass

    # Get DB filter settings
    filters = {f["plex_username"]: f["enabled"] for f in await db.get_all_user_filters()}

    # Merge: if username not in DB, default enabled=True
    result = []
    seen = set()
    for u in plex_users:
        name = u["username"]
        seen.add(name)
        result.append({
            "username": name,
            "thumb": u.get("thumb", ""),
            "enabled": bool(filters.get(name, 1)),
        })

    # Also include any DB entries not returned by Plex
    for name, enabled in filters.items():
        if name not in seen:
            result.append({"username": name, "thumb": "", "enabled": bool(enabled)})

    return {"users": result}


@router.put("/{username}")
async def update_user_filter(username: str, payload: UserFilterUpdate):
    await db.upsert_user_filter(username, payload.enabled)
    return {"ok": True}
