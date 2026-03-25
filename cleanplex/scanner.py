"""Background video scanner: extracts frames and runs NudeNet inference."""

from __future__ import annotations

import asyncio
import io
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from .frame_extractor import extract_frame, get_duration_ms
from .logger import get_logger
from . import database as db

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)

# Lazy-import NudeNet to avoid slow startup when not scanning
_nude_detector = None
_thread_local = threading.local()
_model_download_lock = threading.Lock()

THUMBNAILS_DIR: Path = Path.home() / ".cleanplex" / "thumbnails"
MODELS_DIR: Path = Path.home() / ".cleanplex" / "models"
NUDENET_640_MODEL_FILENAME = "640m.onnx"
NUDENET_640_DOWNLOAD_URLS = [
    "https://github.com/notAI-tech/NudeNet/releases/download/v3/640m.onnx",
    "https://github.com/notAI-tech/NudeNet/releases/latest/download/640m.onnx",
]

_scan_queue: asyncio.Queue[str] = asyncio.Queue()
_force_scan_queue: asyncio.Queue[str] = asyncio.Queue()
_paused: bool = False
_current_guids: set[str] = set()
_queued_normal: set[str] = set()
_queued_force: set[str] = set()
_queue_wakeup_event: asyncio.Event = asyncio.Event()
_skip_requested_guids: set[str] = set()
_worker_pool_size: int = 1
_restart_requested: bool = False


def get_queue_size() -> int:
    return _scan_queue.qsize() + _force_scan_queue.qsize()


def get_worker_pool_size() -> int:
    return _worker_pool_size


async def request_scanner_restart() -> None:
    """Signal scanner to restart worker pool with updated config."""
    global _restart_requested
    _restart_requested = True
    logger.info("Scanner restart requested")


def get_current_scan() -> str | None:
    if not _current_guids:
        return None
    return sorted(_current_guids)[0]


def get_current_scans() -> list[str]:
    return sorted(_current_guids)


def pause_scanner() -> None:
    global _paused
    _paused = True
    logger.info("Scanner paused")


def resume_scanner() -> None:
    global _paused
    _paused = False
    logger.info("Scanner resumed")


async def force_scan_job(plex_guid: str) -> None:
    """
    Prioritize a title for immediate scanning by moving it to the force-scan queue.
    
    Ensures the force-scan flag is set in the database and the title is moved to
    the high-priority force queue (even if it was already in the normal queue).
    Workers always check the force queue first, so this title will be scanned
    as soon as a worker is available.
    """
    await db.set_force_scan(plex_guid, True)
    if plex_guid in _current_guids:
        logger.info("Force scan requested for %s, already scanning", plex_guid)
        return
    if plex_guid in _queued_force:
        logger.info("Force scan requested for %s, already at top priority", plex_guid)
        return
    # Remove from normal queue if already there, so it goes to force queue instead.
    # This ensures "Scan Now" actually prioritizes the title by moving it ahead of
    # other pending scans in the normal queue.
    if plex_guid in _queued_normal:
        _queued_normal.discard(plex_guid)
        logger.info("Moved %s from normal queue to force queue", plex_guid)
    await _force_scan_queue.put(plex_guid)
    _queued_force.add(plex_guid)
    _queue_wakeup_event.set()
    logger.warning(f"Force scan activated for {plex_guid} - will scan immediately")


def is_paused() -> bool:
    return _paused


async def enqueue(plex_guid: str) -> None:
    if plex_guid in _current_guids:
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


def _get_detector(model_name: str = "320n", model_path: str = ""):
    model_name_normalized = (model_name or "320n").strip().lower()
    detector = getattr(_thread_local, "nude_detector", None)
    detector_key = getattr(_thread_local, "nude_detector_key", None)

    # 640m can be downloaded automatically to local app storage when needed.
    requested_resolution = 640 if model_name_normalized.startswith("640") else 320
    selected_model_path = (model_path or "").strip()
    if requested_resolution == 640 and not selected_model_path:
        selected_model_path = _ensure_local_640m_model()
        if not selected_model_path:
            logger.warning("Could not prepare 640m model; falling back to bundled 320n")
            requested_resolution = 320

    if selected_model_path and not os.path.isfile(selected_model_path):
        logger.warning(
            "Configured NudeNet model path not found: %s; falling back to bundled 320n",
            selected_model_path,
        )
        selected_model_path = ""
        requested_resolution = 320

    current_key = (requested_resolution, selected_model_path)
    if detector is None or detector_key != current_key:
        try:
            from nudenet import NudeDetector

            kwargs = {"inference_resolution": requested_resolution}
            if selected_model_path:
                kwargs["model_path"] = selected_model_path
            detector = NudeDetector(**kwargs)
            _thread_local.nude_detector = detector
            _thread_local.nude_detector_key = current_key
            logger.info("NudeNet detector loaded in thread %s", threading.get_ident())
        except ImportError:
            logger.error("nudenet package not installed. Run: pip install nudenet")
            raise
    return detector


def _ensure_local_640m_model() -> str:
    """Ensure 640m model exists locally and return its path, or empty string on failure."""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    target = MODELS_DIR / NUDENET_640_MODEL_FILENAME
    if target.is_file() and target.stat().st_size > 0:
        return str(target)

    with _model_download_lock:
        # Another thread may have completed the download while waiting on lock.
        if target.is_file() and target.stat().st_size > 0:
            return str(target)

        for url in NUDENET_640_DOWNLOAD_URLS:
            try:
                logger.info("Downloading NudeNet 640m model from %s", url)
                with httpx.Client(timeout=120.0, follow_redirects=True) as client:
                    with client.stream("GET", url) as resp:
                        resp.raise_for_status()
                        with open(target, "wb") as out:
                            for chunk in resp.iter_bytes():
                                if chunk:
                                    out.write(chunk)

                if target.is_file() and target.stat().st_size > 0:
                    logger.info("Downloaded NudeNet 640m model to %s", target)
                    return str(target)
            except Exception as exc:
                logger.warning("Failed to download NudeNet 640m model from %s: %s", url, exc)

        if target.exists() and target.stat().st_size == 0:
            target.unlink(missing_ok=True)
        return ""


def _classify_frame(
    jpeg_bytes: bytes,
    threshold: float,
    enabled_labels: set[str],
    model_name: str,
    model_path: str,
) -> tuple[bool, float, list[str]]:
    """Return (is_nude, confidence, detected_labels) for a JPEG frame.
    
    Only returns labels that are in enabled_labels.
    Only returns is_nude=True if any enabled label meets threshold.
    """
    try:
        import tempfile, os
        detector = _get_detector(model_name=model_name, model_path=model_path)

        # NudeNet works on file paths; write to a temp file
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(jpeg_bytes)
            tmp_path = f.name

        try:
            results = detector.detect(tmp_path)
        finally:
            os.unlink(tmp_path)

        if not results:
            return False, 0.0, []

        max_score = 0.0
        detected = []
        
        # Only process labels that are enabled
        for det in results:
            label = det.get("class")
            score = det.get("score", 0.0)
            
            # Only include if this label is enabled
            if label in enabled_labels:
                if label not in detected:
                    detected.append(label)
                if score > max_score:
                    max_score = score

        # Return is_nude only if we found enabled labels above threshold
        return max_score >= threshold, max_score, detected
    except Exception as exc:
        logger.debug("Classification error: %s", exc)
        return False, 0.0, []


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
    current = get_current_scan()
    if current is None:
        return
    request_skip_scan(current)


def request_skip_scan(plex_guid: str) -> bool:
    """Request that a specific active scan title be aborted and left as pending."""
    if plex_guid not in _current_guids:
        return False
    _skip_requested_guids.add(plex_guid)
    logger.info("Skip requested for active scan: %s", plex_guid)
    return True


async def scan_video(plex_guid: str, config) -> None:
    _skip_requested_guids.discard(plex_guid)

    job = await db.get_scan_job_by_guid(plex_guid)
    if not job:
        logger.warning("No scan job found for guid %s", plex_guid)
        return

    # Check if title is marked as ignored
    is_ignored = bool(job.get("ignored", 0))
    if is_ignored:
        logger.info("Skipping ignored title: %s", job["title"])
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

    _current_guids.add(plex_guid)
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
        cluster_detected_labels: list[str] = []

        threshold = config.confidence_threshold
        enabled_labels = set(config.scan_labels) if config.scan_labels else set()
        nudenet_model = str(getattr(config, "nudenet_model", "320n"))
        nudenet_model_path = str(getattr(config, "nudenet_model_path", ""))

        THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)

        async def _flush_cluster() -> None:
            nonlocal cluster_start_ms, cluster_prev_ms, cluster_hit_count
            nonlocal cluster_best_jpeg, cluster_best_score, cluster_detected_labels, segments_inserted

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

                labels_str = ",".join(cluster_detected_labels)
                await db.insert_segment(
                    plex_guid=plex_guid,
                    title=title,
                    start_ms=cluster_start_ms,
                    end_ms=end_ms,
                    confidence=cluster_best_score,
                    thumbnail_path=thumb_path,
                    labels=labels_str,
                )
                segments_inserted += 1

            cluster_start_ms = None
            cluster_prev_ms = 0
            cluster_hit_count = 0
            cluster_best_jpeg = b""
            cluster_best_score = 0.0
            cluster_detected_labels = []

        for idx, offset_ms in enumerate(range(0, duration_ms, step_ms)):
            # User requested skip of this title — leave it as pending (not re-scanned).
            if plex_guid in _skip_requested_guids:
                _skip_requested_guids.discard(plex_guid)
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
                is_nude, score, detected_labels = await asyncio.to_thread(
                    _classify_frame,
                    jpeg,
                    threshold,
                    enabled_labels,
                    nudenet_model,
                    nudenet_model_path,
                )
                if is_nude:
                    if cluster_start_ms is None:
                        cluster_start_ms = offset_ms
                        cluster_prev_ms = offset_ms
                        cluster_hit_count = 1
                        cluster_best_jpeg = jpeg
                        cluster_best_score = score
                        cluster_detected_labels = detected_labels.copy()
                    elif offset_ms - cluster_prev_ms > gap_ms:
                        # Previous segment is complete; persist it immediately.
                        await _flush_cluster()
                        cluster_start_ms = offset_ms
                        cluster_prev_ms = offset_ms
                        cluster_hit_count = 1
                        cluster_best_jpeg = jpeg
                        cluster_best_score = score
                        cluster_detected_labels = detected_labels.copy()
                    else:
                        cluster_prev_ms = offset_ms
                        cluster_hit_count += 1
                        if score > cluster_best_score:
                            cluster_best_jpeg = jpeg
                            cluster_best_score = score
                            cluster_detected_labels = detected_labels.copy()
                        else:
                            # Merge labels from lower-confidence detections
                            for label in detected_labels:
                                if label not in cluster_detected_labels:
                                    cluster_detected_labels.append(label)

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
        _current_guids.discard(plex_guid)


async def _scanner_worker_loop(worker_id: int, get_config_fn) -> None:
    """Single scanner worker — scans queued jobs and respects pause/scan window."""
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

        logger.info(
            "Worker %d starting scan of %s (force_scan=%s, in_window=%s)",
            worker_id,
            plex_guid,
            is_force_scan,
            in_window,
        )
        await scan_video(plex_guid, config)
        
        # Clear force_scan flag after job completes
        if is_force_scan:
            await db.set_force_scan(plex_guid, False)
            logger.info(f"Cleared force_scan for {plex_guid}")

        await asyncio.sleep(1)  # Brief pause between scans


async def scanner_loop(get_config_fn) -> None:
    """Main scanner supervisor — runs multiple scanner workers concurrently."""
    global _worker_pool_size, _restart_requested
    await enqueue_pending()

    while True:
        config = await get_config_fn()
        worker_count = max(1, int(getattr(config, "scan_workers", 2)))
        _worker_pool_size = worker_count
        _restart_requested = False
        logger.info("Starting scanner pool with %d worker(s)", worker_count)

        # Create worker tasks
        worker_tasks = [
            asyncio.create_task(_scanner_worker_loop(i + 1, get_config_fn))
            for i in range(worker_count)
        ]

        try:
            # Run workers until restart or unexpected completion
            while not _restart_requested:
                done, pending = await asyncio.wait(
                    worker_tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if done:
                    # A worker unexpectedly completed; restart the pool
                    logger.warning("Worker task completed unexpectedly, restarting pool")
                    break

            # Restart requested or worker failed; cancel all tasks
            logger.info("Shutting down worker pool for restart")
            for task in worker_tasks:
                if not task.done():
                    task.cancel()
            # Wait for all tasks to complete/cancel
            try:
                await asyncio.gather(*worker_tasks)
            except asyncio.CancelledError:
                pass
            # Loop continues, which will pick up config changes and restart

        except Exception as exc:
            logger.error("Scanner pool error: %s", exc)
            for task in worker_tasks:
                if not task.done():
                    task.cancel()
            try:
                await asyncio.gather(*worker_tasks)
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(5)  # Backoff before retry
