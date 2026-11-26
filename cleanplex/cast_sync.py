"""Background job that copies each title's cast from Plex into the database for the Library actor filter."""

from __future__ import annotations

import asyncio

from . import database as db
from .logger import get_logger

logger = get_logger(__name__)

BATCH = 200            # keys per round; Plex is asked in chunks of plex_client.CAST_CHUNK
PAUSE_BETWEEN_S = 5    # between rounds, so a first sync of a large library does not hammer Plex
IDLE_S = 600           # nothing left to sync: look again for new titles after this long


PHOTOS_MARK = "cast_photos_v1"  # set once every title was queued again to pick up actor photos (#227)


async def sync_cast_once(get_client_fn) -> int:
    """Sync one batch of titles with their actors' photos; return how many were stored (0: nothing left)."""
    if await db.get_setting(PHOTOS_MARK, "") != "1":
        # Casts synced before photos were kept have none; fetch every title once more.
        await db.reset_cast_synced()
        await db.set_setting(PHOTOS_MARK, "1")
    keys = await db.get_cast_keys_to_sync(BATCH)
    if not keys:
        return 0
    photos: dict[str, str] = {}
    cast = await get_client_fn().get_cast(keys, photos=photos)
    await db.replace_cast(cast)
    await db.put_actor_photos(photos)
    return len(cast)


async def cast_sync_loop(get_config_fn, get_client_fn) -> None:
    """Keep title_cast current: drain unsynced titles in batches, then idle."""
    while True:
        delay = IDLE_S
        try:
            if (await get_config_fn()).is_configured():
                stored = await sync_cast_once(get_client_fn)
                if stored:
                    logger.info("Synced cast for %d title(s)", stored)
                    delay = PAUSE_BETWEEN_S
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Cast sync error: %s", exc)
        await asyncio.sleep(delay)
