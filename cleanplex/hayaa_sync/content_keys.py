"""Resolving and persisting Hayaa content keys for live and removed titles (#234)."""

from __future__ import annotations

import asyncio
import os

from .. import database as db
from .. import hayaa
from ..logger import get_logger
from .models import SyncTitle

logger = get_logger(__name__)


class ContentKeyStore:
    """Computes missing content keys once from disk and loads kept keys of removed titles."""

    async def resolve_live(self, titles: list[SyncTitle]) -> dict[str, SyncTitle]:
        """Return {content_key: SyncTitle}. Only titles without a stored key touch their file."""
        out: dict[str, SyncTitle] = {}
        for t in titles:
            key = t.content_key
            if not key:
                path = t.file_path
                try:
                    if not await asyncio.to_thread(os.path.isfile, path):
                        continue
                    vid = t.version_id or None
                    key = await hayaa.content_key_for(path, version_id=vid)
                    st = await asyncio.to_thread(os.stat, path)
                    size, mtime = st.st_size, int(st.st_mtime)
                except (OSError, ValueError) as exc:
                    logger.debug("No content key for %s: %s", path, exc)
                    continue
                await db.set_title_content_key(t.plex_guid, key, size, mtime, version_id=vid)
                t = SyncTitle(
                    plex_guid=t.plex_guid,
                    version_id=t.version_id,
                    title=t.title,
                    file_path=t.file_path,
                    duration_ms=t.duration_ms,
                    status=t.status,
                    scan_version=t.scan_version,
                    finished_at=t.finished_at,
                    content_key=key,
                    content_size=size,
                    content_mtime=mtime,
                    hayaa_sync_state=t.hayaa_sync_state,
                    removed=False,
                )
            out[key] = t
        return out

    async def load_removed(self, live_keys: set[str]) -> dict[str, SyncTitle]:
        """Return {content_key: SyncTitle} for removed titles whose key was kept (#228)."""
        rows = await db.get_hayaa_removed_titles()
        return {
            r["content_key"]: SyncTitle.from_row(r, removed=True)
            for r in rows
            if r["content_key"] not in live_keys
        }
