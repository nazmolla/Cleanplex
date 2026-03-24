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
_paused: bool = False
_current_guid: str | None = None
_force_scan: bool = False


def get_queue_size() -> int:
    return _scan_queue.qsize()


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


def force_scan_now() -> None:
    global _paused, _force_scan
    _paused = False
    _force_scan = True
    logger.warning("FORCE SCAN ACTIVATED - bypassing time window restrictions")


def is_paused() -> bool:
    return _paused


async def enqueue(plex_guid: str) -> None:
    await _scan_queue.put(plex_guid)


async def enqueue_pending() -> None:
    """Push all pending scan jobs onto the queue."""
    jobs = await db.get_scan_jobs(status="pending")
    for job in jobs:
        await _scan_queue.put(job["plex_guid"])
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


def _classify_frame(jpeg_bytes: bytes, threshold: float) -> tuple[bool, float]:
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

        # Look for nudity-related labels
        nude_labels = {
            "FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED",
            "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED",
            "BUTTOCKS_EXPOSED", "FEMALE_BREAST_COVERED",
        }
        max_score = 0.0
        for det in results:
            if det.get("class") in nude_labels:
                score = det.get("score", 0.0)
                if score > max_score:
                    max_score = score

        return max_score >= threshold, max_score
    except Exception as exc:
        logger.debug("Classification error: %s", exc)
        return False, 0.0


def _cluster_frames(flagged_ms: list[int], gap_ms: int = 15_000) -> list[tuple[int, int]]:
    """Merge consecutive flagged frame offsets into (start_ms, end_ms) segments."""
    if not flagged_ms:
        return []
    flagged_ms = sorted(flagged_ms)
    segments = []
    start = flagged_ms[0]
    prev = flagged_ms[0]
    for ms in flagged_ms[1:]:
        if ms - prev > gap_ms:
            segments.append((start, prev + gap_ms))
            start = ms
        prev = ms
    segments.append((start, prev + gap_ms))
    return segments


async def scan_video(plex_guid: str, config) -> None:
    global _current_guid

    job = await db.get_scan_job_by_guid(plex_guid)
    if not job:
        logger.warning("No scan job found for guid %s", plex_guid)
        return

    file_path = job["file_path"]
    title = job["title"]

    if not os.path.isfile(file_path):
        logger.error("Video file not found: %s", file_path)
        await db.update_scan_job_status(plex_guid, "failed", error_msg="File not found")
        return

    _current_guid = plex_guid
    await db.update_scan_job_status(plex_guid, "scanning", progress=0.0)
    logger.info("Scanning: %s", title)

    try:
        duration_ms = await get_duration_ms(file_path)
        if not duration_ms:
            raise RuntimeError("Could not determine video duration")

        step_ms = 10_000  # 1 frame per 10 seconds
        total_steps = max(1, duration_ms // step_ms)
        flagged: list[int] = []
        best_frames: dict[int, tuple[bytes, float]] = {}  # offset_ms -> (jpeg, score)

        threshold = config.confidence_threshold

        for idx, offset_ms in enumerate(range(0, duration_ms, step_ms)):
            if _paused:
                # Re-queue for later
                await db.update_scan_job_status(plex_guid, "pending", progress=idx / total_steps)
                await _scan_queue.put(plex_guid)
                logger.info("Scan paused mid-way through %s, re-queued", title)
                return

            jpeg = await extract_frame(file_path, offset_ms)
            if jpeg:
                is_nude, score = await asyncio.to_thread(_classify_frame, jpeg, threshold)
                if is_nude:
                    flagged.append(offset_ms)
                    best_frames[offset_ms] = (jpeg, score)

            progress = (idx + 1) / total_steps
            if idx % 30 == 0:  # Update DB every 5 minutes of video
                await db.update_scan_job_status(plex_guid, "scanning", progress=progress)

        # Build segments from flagged frames
        segments = _cluster_frames(flagged)
        THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)

        for start_ms, end_ms in segments:
            # Find the best (highest confidence) frame for the thumbnail
            candidates = [(ms, best_frames[ms]) for ms in flagged if start_ms <= ms <= end_ms]
            if candidates:
                best_ms, (best_jpeg, best_score) = max(candidates, key=lambda x: x[1][1])
            else:
                best_score = 0.0
                best_jpeg = b""

            thumb_path = ""
            if best_jpeg:
                thumb_filename = f"{plex_guid.replace('/', '_')}_{start_ms}.jpg"
                thumb_full = THUMBNAILS_DIR / thumb_filename
                thumb_full.write_bytes(best_jpeg)
                thumb_path = str(thumb_full)

            await db.insert_segment(
                plex_guid=plex_guid,
                title=title,
                start_ms=start_ms,
                end_ms=end_ms,
                confidence=best_score,
                thumbnail_path=thumb_path,
            )

        await db.update_scan_job_status(plex_guid, "done", progress=1.0)
        logger.info("Scan complete: %s — found %d segment(s)", title, len(segments))

    except Exception as exc:
        logger.error("Scan failed for %s: %s", title, exc)
        await db.update_scan_job_status(plex_guid, "failed", error_msg=str(exc))
    finally:
        _current_guid = None


async def scanner_loop(get_config_fn) -> None:
    """Main scanner loop — runs forever, respects pause and scan window."""
    global _force_scan
    # On startup, push pending jobs onto the queue
    await enqueue_pending()

    while True:
        config = await get_config_fn()

        if not config.is_scan_window() and not _force_scan:
            if not _paused:
                pause_scanner()
            await asyncio.sleep(60)
            continue
        else:
            if _paused:
                resume_scanner()
            if _force_scan:
                logger.warning(f"Force scan active - processing queue immediately ignoring time window")

        try:
            plex_guid = await asyncio.wait_for(_scan_queue.get(), timeout=30)
        except asyncio.TimeoutError:
            # If we were force-scanning and queue is empty, reset force_scan
            if _force_scan:
                _force_scan = False
                logger.warning("Force scan mode ended - queue emptied")
            continue

        job = await db.get_scan_job_by_guid(plex_guid)
        if not job or job["status"] not in ("pending", "scanning"):
            logger.debug(f"Skipping {plex_guid}: not found or wrong status")
            continue

        logger.info(f"Starting scan of {plex_guid} (force_scan={_force_scan})")
        await scan_video(plex_guid, config)
        _scan_queue.task_done()
        await asyncio.sleep(1)  # Brief pause between scans
