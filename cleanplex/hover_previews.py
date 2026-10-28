"""Evening job rendering the hover previews of segments (#114).

Runs apart from the scanner, inside its own window (hover_window_start/end), and
renders one preview at a time for active segments that have none yet. Each is a
small silent MP4 in HOVER_DIR named after the segment id; segments are never
edited in place (a rescan writes new rows), so a file never goes stale and is
only deleted once its segment is gone.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Awaitable, Callable

from . import database as db
from . import frame_extractor
from .config import Config
from .logger import get_logger

logger = get_logger(__name__)

HOVER_DIR: Path = Path.home() / ".cleanplex" / "hover"
# How often to check whether the window has opened.
CHECK_INTERVAL_S = 300
# Segments fetched per query; the window is re-checked between batches.
BATCH_SIZE = 25


def hover_path(segment_id: int) -> Path:
    return HOVER_DIR / f"{segment_id}.mp4"


def prune_orphans(segment_ids: set[int]) -> int:
    """Delete preview files (and leftover .part files) whose segment is gone."""
    removed = 0
    for path in HOVER_DIR.glob("*.mp4*"):
        stem = path.name.split(".", 1)[0]
        if path.name.endswith(".part") or not stem.isdigit() or int(stem) not in segment_ids:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


async def render_pending(should_continue: Callable[[], Awaitable[bool]]) -> int:
    """Render previews for pending segments while *should_continue* holds; return how many."""
    HOVER_DIR.mkdir(parents=True, exist_ok=True)
    removed = prune_orphans(await db.get_segment_ids())
    retried = await db.reset_missing_hover_previews()
    if removed or retried:
        logger.info("Hover previews: removed %d orphaned files, retrying %d unreachable", removed, retried)

    rendered = 0
    while await should_continue():
        # Every candidate leaves the pending set below, so this loop always ends.
        batch = await db.get_hover_preview_candidates(BATCH_SIZE)
        if not batch:
            break
        for cand in batch:
            if not await should_continue():
                break
            file_path = cand.get("file_path") or ""
            if not file_path or not await asyncio.to_thread(os.path.isfile, file_path):
                # A share may be offline; 'missing' is reset at the start of each run.
                await db.set_hover_status(cand["id"], "missing")
                continue
            ok = await frame_extractor.render_hover_clip(
                file_path, cand["start_ms"], cand["end_ms"] - cand["start_ms"], hover_path(cand["id"]),
            )
            await db.set_hover_status(cand["id"], "done" if ok else "failed")
            rendered += ok
    if rendered:
        logger.info("Hover previews: rendered %d", rendered)
    return rendered


async def hover_preview_loop(get_config_fn: Callable[[], Awaitable[Config]]) -> None:
    """Run render_pending whenever the hover window is open and the job is enabled."""

    async def in_window() -> bool:
        config = await get_config_fn()
        return config.hover_previews_enabled and config.is_hover_window()

    while True:
        try:
            if await in_window():
                await render_pending(in_window)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let one bad file or DB hiccup end the job for good.
            logger.exception("Hover preview run failed")
        await asyncio.sleep(CHECK_INTERVAL_S)
