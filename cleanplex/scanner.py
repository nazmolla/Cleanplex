"""Background video scanner: extracts frames and runs NudeNet inference."""

from __future__ import annotations

import asyncio
import io
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .frame_extractor import extract_frame, get_duration_ms
from .logger import get_logger
from . import database as db

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)

# Lazy-import NudeNet to avoid slow startup when not scanning
_nude_detector = None

THUMBNAILS_DIR: Path = Path.home() / ".cleanplex" / "thumbnails"

_scan_queue: asyncio.Queue[str] = asyncio.Queue()
_force_scan_queue: asyncio.Queue[str] = asyncio.Queue()
_paused: bool = False
_current_guid: str | None = None
_queued_normal: set[str] = set()
_queued_force: set[str] = set()
_queue_wakeup_event: asyncio.Event = asyncio.Event()
_skip_current_scan: bool = False


def get_queue_size() -> int:
    return _scan_queue.qsize() + _force_scan_queue.qsize()


def get_current_scan() -> str | None:
    return _current_guid


def pause_scanner() -> None:
    global _paused
    _paused = True
    logger.info("Scanner paused")


def resume_scanner() -> None:
    global _paused
    _paused = False
    logger.info("Scanner resumed")


async def force_scan_job(plex_guid: str) -> None:
    """Set force_scan flag for a specific job and queue it."""
    await db.set_force_scan(plex_guid, True)
    if _current_guid == plex_guid:
        logger.info("Force scan requested for %s, already scanning", plex_guid)
        return
    if plex_guid in _queued_force:
        logger.info("Force scan requested for %s, already at top priority", plex_guid)
        return
    await _force_scan_queue.put(plex_guid)
    _queued_force.add(plex_guid)
    _queue_wakeup_event.set()
    logger.warning(f"Force scan activated for {plex_guid} - will scan immediately")


def is_paused() -> bool:
    return _paused


async def enqueue(plex_guid: str) -> None:
    if _current_guid == plex_guid:
        logger.debug("Skipping enqueue for %s: already scanning", plex_guid)
        return
    if plex_guid in _queued_force or plex_guid in _queued_normal:
        logger.debug("Skipping enqueue for %s: already queued", plex_guid)
        return
    await _scan_queue.put(plex_guid)
    _queued_normal.add(plex_guid)
    _queue_wakeup_event.set()


async def enqueue_pending() -> None:
    """Push all pending scan jobs onto the queue."""
    jobs = await db.get_scan_jobs(status="pending")
    for job in jobs:
        await enqueue(job["plex_guid"])
    if jobs:
        logger.info("Queued %d pending scan jobs", len(jobs))


def _get_detector():
    global _nude_detector
    if _nude_detector is None:
        try:
            from nudenet import NudeDetector
            _nude_detector = NudeDetector()
            logger.info("NudeNet detector loaded")
        except ImportError:
            logger.error("nudenet package not installed. Run: pip install nudenet")
            raise
    return _nude_detector


def _classify_frame(
    jpeg_bytes: bytes,
    threshold: float,
    enabled_labels: set[str],
) -> tuple[bool, float]:
    """Return (is_nude, confidence) for a JPEG frame."""
    try:
        import tempfile, os
        detector = _get_detector()

        # NudeNet works on file paths; write to a temp file
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(jpeg_bytes)
            tmp_path = f.name

        try:
            results = detector.detect(tmp_path)
        finally:
            os.unlink(tmp_path)

        if not results:
            return False, 0.0

        max_score = 0.0
        for det in results:
            if det.get("class") in enabled_labels:
                score = det.get("score", 0.0)
                if score > max_score:
                    max_score = score

        return max_score >= threshold, max_score
    except Exception as exc:
        logger.debug("Classification error: %s", exc)
        return False, 0.0


def _cluster_frames(
    flagged_ms: list[int],
    gap_ms: int = 15_000,
    min_hits: int = 1,
) -> list[tuple[int, int]]:
    """Merge consecutive flagged frame offsets into (start_ms, end_ms) segments."""
    if not flagged_ms:
        return []
    flagged_ms = sorted(flagged_ms)
    segments = []
    start = flagged_ms[0]
    prev = flagged_ms[0]
    hit_count = 1
    for ms in flagged_ms[1:]:
        if ms - prev > gap_ms:
            if hit_count >= max(1, min_hits):
                segments.append((start, prev + gap_ms))
            start = ms
            hit_count = 1
        else:
            hit_count += 1
        prev = ms
    if hit_count >= max(1, min_hits):
        segments.append((start, prev + gap_ms))
    return segments


def skip_current_scan() -> None:
    """Request that the currently-running scan title be aborted and left as pending."""
    global _skip_current_scan
    _skip_current_scan = True
    logger.info("Skip requested for current scan: %s", _current_guid)


async def scan_video(plex_guid: str, config) -> None:
    global _current_guid, _skip_current_scan
    _skip_current_scan = False  # Reset for each new scan title

    job = await db.get_scan_job_by_guid(plex_guid)
    if not job:
        logger.warning("No scan job found for guid %s", plex_guid)
        return

    # Safety check: Don't scan outside window unless force-scanned
    is_force_scan = bool(job.get("force_scan", 0))
    if not is_force_scan and not config.is_scan_window():
        logger.warning("Attempted to scan outside scan window (not force-scanned): %s. Re-queueing.", job["title"])
        await db.update_scan_job_status(plex_guid, "pending")
        await enqueue(plex_guid)
        return

    file_path = job["file_path"]
    title = job["title"]

    if not os.path.isfile(file_path):
        logger.error("Video file not found: %s", file_path)
        await db.update_scan_job_status(plex_guid, "failed", error_msg="File not found")
        return

    _current_guid = plex_guid
    # Fresh scan run: clear prior segments so retries/re-scans do not duplicate entries.
    deleted = await db.delete_segments_for_guid(plex_guid)
    if deleted:
        logger.info("Cleared %d previous segment(s) for %s", deleted, title)
    await db.update_scan_job_status(plex_guid, "scanning", progress=0.0)
    logger.info("Scanning: %s", title)

    try:
        duration_ms = await get_duration_ms(file_path)
        if not duration_ms:
            raise RuntimeError("Could not determine video duration")

        # Smaller interval improves recall for short scenes; configurable in settings.
        step_ms = max(1000, int(getattr(config, "scan_step_ms", 5000)))
        total_steps = max(1, duration_ms // step_ms)
        gap_ms = max(1000, int(getattr(config, "segment_gap_ms", 12000)))
        min_hits = max(1, int(getattr(config, "segment_min_hits", 1)))
        segments_inserted = 0

        cluster_start_ms: int | None = None
        cluster_prev_ms: int = 0
        cluster_hit_count = 0
        cluster_best_jpeg: bytes = b""
        cluster_best_score = 0.0

        threshold = config.confidence_threshold
        enabled_labels = set(getattr(config, "scan_labels", []) or [])
        if not enabled_labels:
            enabled_labels = {
                "FEMALE_BREAST_EXPOSED",
                "FEMALE_GENITALIA_EXPOSED",
                "MALE_GENITALIA_EXPOSED",
                "ANUS_EXPOSED",
                "BUTTOCKS_EXPOSED",
            }

        THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)

        async def _flush_cluster() -> None:
            nonlocal cluster_start_ms, cluster_prev_ms, cluster_hit_count
            nonlocal cluster_best_jpeg, cluster_best_score, segments_inserted

            if cluster_start_ms is None:
                return

            if cluster_hit_count >= min_hits:
                end_ms = cluster_prev_ms + gap_ms
                thumb_path = ""
                if cluster_best_jpeg:
                    thumb_filename = f"{plex_guid.replace('/', '_')}_{cluster_start_ms}.jpg"
                    thumb_full = THUMBNAILS_DIR / thumb_filename
                    thumb_full.write_bytes(cluster_best_jpeg)
                    thumb_path = str(thumb_full)

                await db.insert_segment(
                    plex_guid=plex_guid,
                    title=title,
                    start_ms=cluster_start_ms,
                    end_ms=end_ms,
                    confidence=cluster_best_score,
                    thumbnail_path=thumb_path,
                )
                segments_inserted += 1

            cluster_start_ms = None
            cluster_prev_ms = 0
            cluster_hit_count = 0
            cluster_best_jpeg = b""
            cluster_best_score = 0.0

        for idx, offset_ms in enumerate(range(0, duration_ms, step_ms)):
            # User requested skip of this title — leave it as pending (not re-scanned).
            if _skip_current_scan:
                _skip_current_scan = False
                logger.info("Scan of '%s' skipped by user request", title)
                await db.update_scan_job_status(plex_guid, "pending", progress=0.0)
                return

            if _paused:
                # Re-queue for later
                await db.update_scan_job_status(plex_guid, "pending", progress=idx / total_steps)
                await enqueue(plex_guid)
                logger.info("Scan paused mid-way through %s, re-queued", title)
                return

            # Periodically check the scan window; abort if it has ended.
            if idx % 5 == 0 and not is_force_scan and not config.is_scan_window():
                await db.update_scan_job_status(plex_guid, "pending", progress=idx / total_steps)
                await enqueue(plex_guid)
                logger.info("Scan window ended during scan of '%s', re-queued", title)
                if not _paused:
                    pause_scanner()
                return

            jpeg = await extract_frame(file_path, offset_ms)
            if jpeg:
                is_nude, score = await asyncio.to_thread(_classify_frame, jpeg, threshold, enabled_labels)
                if is_nude:
                    if cluster_start_ms is None:
                        cluster_start_ms = offset_ms
                        cluster_prev_ms = offset_ms
                        cluster_hit_count = 1
                        cluster_best_jpeg = jpeg
                        cluster_best_score = score
                    elif offset_ms - cluster_prev_ms > gap_ms:
                        # Previous segment is complete; persist it immediately.
                        await _flush_cluster()
                        cluster_start_ms = offset_ms
                        cluster_prev_ms = offset_ms
                        cluster_hit_count = 1
                        cluster_best_jpeg = jpeg
                        cluster_best_score = score
                    else:
                        cluster_prev_ms = offset_ms
                        cluster_hit_count += 1
                        if score > cluster_best_score:
                            cluster_best_jpeg = jpeg
                            cluster_best_score = score

            progress = (idx + 1) / total_steps
            if idx % 30 == 0:  # Update DB every 5 minutes of video
                await db.update_scan_job_status(plex_guid, "scanning", progress=progress)

        # Flush any trailing cluster at the end of scan.
        await _flush_cluster()

        await db.update_scan_job_status(plex_guid, "done", progress=1.0)
        logger.info("Scan complete: %s — found %d segment(s)", title, segments_inserted)

    except Exception as exc:
        logger.error("Scan failed for %s: %s", title, exc)
        await db.update_scan_job_status(plex_guid, "failed", error_msg=str(exc))
    finally:
        _current_guid = None


async def scanner_loop(get_config_fn) -> None:
    """Main scanner loop — runs forever, respects pause and scan window."""
    # On startup, push pending jobs onto the queue
    await enqueue_pending()

    while True:
        config = await get_config_fn()

        try:
            # Prioritize explicit force-scan requests ahead of normal queue items.
            try:
                plex_guid = _force_scan_queue.get_nowait()
                _queued_force.discard(plex_guid)
            except asyncio.QueueEmpty:
                plex_guid = await asyncio.wait_for(_scan_queue.get(), timeout=30)
                _queued_normal.discard(plex_guid)
        except asyncio.TimeoutError:
            # Queue is empty, respect scan window
            if not config.is_scan_window():
                if not _paused:
                    pause_scanner()
            else:
                if _paused:
                    resume_scanner()
            try:
                await asyncio.wait_for(_queue_wakeup_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            _queue_wakeup_event.clear()
            continue

        job = await db.get_scan_job_by_guid(plex_guid)
        if not job or job["status"] not in ("pending", "scanning"):
            logger.debug(f"Skipping {plex_guid}: not found or wrong status")
            continue

        # Check if this job should run: is_force_scan OR within scan window
        is_force_scan = bool(job.get("force_scan", 0))
        in_window = config.is_scan_window()

        if not is_force_scan and not in_window:
            # Outside window and not force-scan, re-queue and wait
            logger.debug(f"Job {plex_guid} outside scan window and not force-scan, re-queuing")
            await enqueue(plex_guid)
            if not _paused:
                pause_scanner()
            # If a force-scan job arrived, don't sleep; process it immediately.
            if not _force_scan_queue.empty():
                continue
            try:
                await asyncio.wait_for(_queue_wakeup_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            _queue_wakeup_event.clear()
            continue

        if _paused:
            resume_scanner()

        logger.info(f"Starting scan of {plex_guid} (force_scan={is_force_scan}, in_window={in_window})")
        await scan_video(plex_guid, config)
        
        # Clear force_scan flag after job completes
        if is_force_scan:
            await db.set_force_scan(plex_guid, False)
            logger.info(f"Cleared force_scan for {plex_guid}")

        await asyncio.sleep(1)  # Brief pause between scans
