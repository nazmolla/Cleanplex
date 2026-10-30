"""Files that belong to segments (thumbnails, hover previews, feedback frames) and their cleanup (#153).

A segment's files are removed when the segment is deleted, and a periodic sweep removes
whatever no segment or feedback row references any more, so the directories cannot grow
without bound.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from . import database as db
from . import hover_previews
from .logger import get_logger

logger = get_logger(__name__)

# Frames of segments marked false positives or relabelled, copied when the verdict is recorded;
# the feedback rows own them (they outlive the segment on purpose), so the sweep keeps referenced ones.
FEEDBACK_DIR: Path = Path.home() / ".cleanplex" / "feedback"
# A scan writes a thumbnail before it inserts the segment row, so a file younger than this may
# belong to a row about to be written and is never swept.
GRACE_S = 3600
# A segment whose title no longer exists is kept this long, in case the title comes back.
ORPHAN_SEGMENT_DAYS = 30
SWEEP_INTERVAL_S = 6 * 3600
STARTUP_DELAY_S = 120  # let the service settle before the first sweep


def _unlink(path: Path | str) -> int:
    """Delete one file; return its size, or 0 if it was already gone."""
    try:
        size = os.path.getsize(path)
        os.unlink(path)
        return size
    except OSError:
        return 0


async def remove_files(refs: list[tuple[int, str]]) -> int:
    """Delete the thumbnail and hover preview of each (segment id, thumbnail path); return bytes freed."""

    def work() -> int:
        freed = 0
        for segment_id, thumbnail in refs:
            if thumbnail:
                freed += _unlink(thumbnail)
            freed += _unlink(hover_previews.hover_path(segment_id))
        return freed

    return await asyncio.to_thread(work)


async def delete_segment(segment_id: int) -> bool:
    """Delete a segment and its files; False if it did not exist."""
    ref = await db.get_segment_file_ref(segment_id)
    deleted = await db.delete_segment(segment_id)
    if deleted and ref:
        await remove_files([ref])
    return deleted


async def delete_segments_for_guid(plex_guid: str) -> int:
    """Delete all of a title's segments and their files; return the row count."""
    refs = await db.get_segment_file_refs_for_guid(plex_guid)
    deleted = await db.delete_segments_for_guid(plex_guid)
    if refs:
        await remove_files(refs)
    return deleted


def _sweep_directory(directory: Path, referenced: set[str], grace_s: float) -> tuple[int, int]:
    """Delete files in *directory* that are not in *referenced* and are older than *grace_s*."""
    if not directory.is_dir():
        return 0, 0
    cutoff = time.time() - grace_s
    removed = freed = 0
    with os.scandir(directory) as entries:
        for entry in entries:
            if not entry.is_file(follow_symlinks=False) or entry.path in referenced:
                continue
            try:
                if entry.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            size = _unlink(entry.path)
            removed += 1
            freed += size
    return removed, freed


async def sweep(thumbnails_dir: Path | None = None, grace_s: float = GRACE_S) -> dict:
    """Remove orphaned segments and the files nothing references; return what was removed.

    1. Segments whose title is gone and that are older than ORPHAN_SEGMENT_DAYS, with their files.
    2. Thumbnails no segment points at, hover previews of segments that no longer exist, and
       feedback frames no feedback row points at (all only if older than the grace period).
    """
    if thumbnails_dir is None:
        from . import scanner

        thumbnails_dir = scanner.THUMBNAILS_DIR
    report = {"orphan_segments": 0, "thumbnails": 0, "hover": 0, "feedback_frames": 0, "bytes": 0}

    orphans = await db.get_orphan_segment_refs(ORPHAN_SEGMENT_DAYS)
    if orphans:
        await db.delete_segments_by_ids([segment_id for segment_id, _ in orphans])
        report["bytes"] += await remove_files(orphans)
        report["orphan_segments"] = len(orphans)

    thumbs, freed = await asyncio.to_thread(
        _sweep_directory, thumbnails_dir, await db.get_referenced_thumbnail_paths(), grace_s
    )
    report["thumbnails"], report["bytes"] = thumbs, report["bytes"] + freed

    # prune_orphans keeps files of every existing segment (false positives included) and drops
    # .part leftovers; a hover file is only ever written for a row that already exists.
    hover_dir = hover_previews.HOVER_DIR
    if hover_dir.is_dir():
        report["hover"] = await asyncio.to_thread(hover_previews.prune_orphans, await db.get_segment_ids())

    frames, freed = await asyncio.to_thread(
        _sweep_directory, FEEDBACK_DIR, await db.get_feedback_frame_paths(), grace_s
    )
    report["feedback_frames"], report["bytes"] = frames, report["bytes"] + freed
    return report


async def segment_cleanup_loop() -> None:
    """Sweep at startup and then every SWEEP_INTERVAL_S; one bad pass never ends the job."""
    await asyncio.sleep(STARTUP_DELAY_S)
    while True:
        try:
            report = await sweep()
            if any(v for k, v in report.items() if k != "bytes"):
                logger.info(
                    "Segment file cleanup: removed %d orphan segment(s), %d thumbnail(s), %d hover preview(s), "
                    "%d feedback frame(s); freed %.1f MB",
                    report["orphan_segments"], report["thumbnails"], report["hover"], report["feedback_frames"],
                    report["bytes"] / 1e6,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Segment file cleanup failed")
        await asyncio.sleep(SWEEP_INTERVAL_S)
