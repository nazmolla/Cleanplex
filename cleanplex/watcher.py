"""Session watcher and library poller."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import datetime

from .logger import get_logger
from . import database as db
from . import filter_engine
from .scanner import enqueue, enqueue_pending, scanner_loop

logger = get_logger(__name__)

# Ring buffer of recent skip events for the dashboard
skip_events: deque[dict] = deque(maxlen=50)


async def session_watcher_loop(get_config_fn, get_client_fn) -> None:
    """Poll Plex sessions every `poll_interval` seconds and fire skips."""
    while True:
        config = await get_config_fn()

        if not config.is_configured():
            await asyncio.sleep(10)
            continue

        try:
            client = get_client_fn()
            sessions = await client.get_active_sessions()

            for session in sessions:
                user_filter = await db.get_user_filter(session.user)
                # Default: filter enabled if no explicit record
                if user_filter is None or user_filter["enabled"]:
                    await filter_engine.process(session, client, config.skip_buffer_ms, config.poll_interval * 1000)

                    # Log skip event if a skip just happened (detect by checking _recently_skipped)
                    sk = filter_engine._recently_skipped.get(session.session_key, 0)
                    if sk and sk > session.position_ms:
                        skip_events.appendleft({
                            "time": datetime.now().isoformat(timespec="seconds"),
                            "user": session.user,
                            "title": session.full_title,
                            "position_ms": session.position_ms,
                            "client": session.client_title,
                        })

        except Exception as exc:
            logger.warning("Session watcher error: %s", exc)

        await asyncio.sleep(config.poll_interval)


async def library_watcher_loop(get_config_fn, get_client_fn) -> None:
    """Periodically check for new Plex library items and enqueue unscanned ones."""
    first_run = True
    while True:
        if not first_run:
            await asyncio.sleep(60)
        first_run = False

        config = await get_config_fn()
        if not config.is_configured():
            continue

        try:
            client = get_client_fn()
            sections = await client.get_library_sections()
            excluded = set(json.loads(await db.get_setting("excluded_library_ids", "[]")))
            scan_ratings = set(json.loads(await db.get_setting("scan_ratings", "[]")))

            for section in sections:
                if section.section_id in excluded:
                    continue
                items = await client.get_library_items(section.section_id)
                for item in items:
                    if not item.file_path:
                        continue
                    if scan_ratings and (item.content_rating or "") not in scan_ratings:
                        continue
                    existing = await db.get_scan_job_by_guid(item.plex_guid)
                    if existing is None:
                        await db.upsert_scan_job(
                            plex_guid=item.plex_guid,
                            title=item.title,
                            file_path=item.file_path,
                            rating_key=item.rating_key,
                            library_id=item.library_id,
                            library_title=item.library_title,
                            content_rating=item.content_rating,
                            media_type=item.media_type,
                            year=item.year,
                            show_guid=item.show_guid,
                        )
                        await enqueue(item.plex_guid)
                        logger.info("New item queued for scan: %s", item.title)

        except Exception as exc:
            logger.warning("Library watcher error: %s", exc)
