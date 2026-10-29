"""Background video scanner: extracts frames and runs NudeNet inference."""

from __future__ import annotations

import asyncio
import io
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import cleanplex.plex_client as plex_mod

from .frame_extractor import extract_frames_batch, extract_frames_window, get_duration_ms
from . import events, siglip_detector
from .logger import get_logger
from . import database as db
from .importers._common import severity_for_labels
from . import ort_runtime

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)

# Lazy-import NudeNet to avoid slow startup when not scanning
_nude_detector = None
_thread_local = threading.local()
# asyncio.Lock guards the async model-download path; threading.local() still owns
# per-thread detector instances (NudeNet is not thread-safe to share).
_model_download_lock = asyncio.Lock()

THUMBNAILS_DIR: Path = Path.home() / ".cleanplex" / "thumbnails"
MODELS_DIR: Path = Path.home() / ".cleanplex" / "models"
# Minimum gap between scan_progress events per scan; the UI bar needs no finer.
PROGRESS_EVENT_INTERVAL_S = 2.0
# A flagged window is re-sampled at refine_step_ms, but never into more frames than this, so a
# long segment costs a bounded amount: 120 frames is about 2 s of NudeNet at 320 px.
REFINE_MAX_FRAMES = 120

# Detection models are cached per thread (threading.local), so the number of
# threads running inference is the number of model copies held in memory. A pool
# sized to scan_workers keeps that at one copy per worker (#103); the default
# asyncio pool has min(32, cpus + 4) threads and ended up loading 16 copies.
_inference_pool: ThreadPoolExecutor | None = None


async def _infer(fn, *args):
    """Run a blocking detection call on the inference pool (default pool if none yet)."""
    pool = _inference_pool
    if pool is None:
        return await asyncio.to_thread(fn, *args)
    return await asyncio.get_running_loop().run_in_executor(pool, fn, *args)


def _replace_inference_pool(size: int | None) -> None:
    """Swap in a pool of *size* threads (None: none), letting the old one wind down."""
    global _inference_pool
    old, _inference_pool = _inference_pool, (
        ThreadPoolExecutor(max_workers=size, thread_name_prefix="cleanplex-infer") if size else None
    )
    if old is not None:
        old.shutdown(wait=False, cancel_futures=True)
NUDENET_640_MODEL_FILENAME = "640m.onnx"
# The weights live under the v3.4-weights release. GitHub's browser download link
# now redirects anonymous clients to a login page (200, HTML), which is how a web
# page ended up saved as the model (#97); _valid_model_file() rejects that. The API
# asset URL with Accept: application/octet-stream still serves the file anonymously.
NUDENET_640_DOWNLOAD_URLS = [
    "https://api.github.com/repos/notAI-tech/NudeNet/releases/assets/176832019",
    "https://github.com/notAI-tech/NudeNet/releases/download/v3.4-weights/640m.onnx",
]
# The real 640m.onnx is ~103 MB; anything far smaller is an error page or truncated.
NUDENET_640_MIN_BYTES = 50 * 1024 * 1024


def _valid_model_file(path: Path) -> bool:
    """True if *path* looks like a complete 640m ONNX model rather than HTML or a stub."""
    try:
        if path.stat().st_size < NUDENET_640_MIN_BYTES:
            return False
        with open(path, "rb") as f:
            head = f.read(64).lstrip()
    except OSError:
        return False
    return not head.startswith(b"<")

_scan_queue: asyncio.Queue[str] = asyncio.Queue()
_force_scan_queue: asyncio.Queue[str] = asyncio.Queue()
_paused: bool = False
_current_guids: set[str] = set()
_queued_normal: set[str] = set()
_queued_force: set[str] = set()
# Ordered lists mirror the sets so callers can expose the queue in execution order.
# Force items always execute before normal items; within each group order is FIFO.
_queued_force_ordered: list[str] = []
_queued_normal_ordered: list[str] = []
# Idle workers sleep on this until a queue has work; every producer notifies all.
# A Condition with a predicate, not an Event: the first worker to wake used to
# clear a shared Event and leave the other idle workers asleep for a minute (#87).
_queue_cond = asyncio.Condition()
_skip_requested_guids: set[str] = set()
_worker_pool_size: int = 1
_restart_requested: bool = False

# Guards all read-check-modify operations on _current_guids, _queued_normal,
# and _queued_force to prevent duplicate or lost entries under concurrent workers.
_state_lock = asyncio.Lock()


def is_scan_eligible(job: dict, excluded_library_ids: set[str], scan_ratings: set[str]) -> bool:
    """Return True if a scan job should enter the normal (non-force) queue.

    A title is eligible when ALL of the following hold:
      - not marked ignored
      - its library is not in the excluded set
      - its content_rating is in scan_ratings, OR scan_ratings is empty (= scan all)

    This is the single source of truth for normal-queue eligibility.
    Apply it everywhere titles are bulk-enqueued (enqueue_pending, scan_library).
    force_scan bypasses this check entirely — callers must not call this for force jobs.
    """
    if job.get("ignored"):
        return False
    if job.get("library_id") in excluded_library_ids:
        return False
    if scan_ratings and (job.get("content_rating") or "").strip() not in scan_ratings:
        return False
    return True


def get_queue_size() -> int:
    return _scan_queue.qsize() + _force_scan_queue.qsize()


def get_ordered_queue_guids() -> tuple[list[str], list[str]]:
    """Return (force_guids, normal_guids) in FIFO execution order.

    Force items will always execute before normal items.
    Snapshot is taken without the lock — callers should treat it as advisory only.
    """
    return list(_queued_force_ordered), list(_queued_normal_ordered)


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
    events.publish("scanner_changed")


def resume_scanner() -> None:
    global _paused
    _paused = False
    logger.info("Scanner resumed")
    events.publish("scanner_changed")


async def force_scan_job(plex_guid: str) -> None:
    """Prioritize a title for immediate scanning by moving it to the force-scan queue.

    DB flag is set outside the lock; queue state mutations are guarded by _state_lock
    to prevent duplicate entries under concurrent API calls.
    """
    await db.set_force_scan(plex_guid, True)
    async with _state_lock:
        if plex_guid in _current_guids:
            logger.info("Force scan requested for %s, already scanning", plex_guid)
            return
        if plex_guid in _queued_force:
            logger.info("Force scan requested for %s, already at top priority", plex_guid)
            return
        # Remove from normal queue if already there, so it goes to force queue instead.
        if plex_guid in _queued_normal:
            _queued_normal.discard(plex_guid)
            if plex_guid in _queued_normal_ordered:
                _queued_normal_ordered.remove(plex_guid)
            logger.info("Moved %s from normal queue to force queue", plex_guid)
        await _force_scan_queue.put(plex_guid)
        _queued_force.add(plex_guid)
        _queued_force_ordered.append(plex_guid)
    await _notify_workers()
    events.publish("scanner_changed")
    logger.warning("Force scan activated for %s — will scan immediately", plex_guid)


async def _notify_workers() -> None:
    """Wake every idle worker so each re-checks the queues."""
    async with _queue_cond:
        _queue_cond.notify_all()


async def _wait_for_work(force_only: bool, timeout: float) -> None:
    """Sleep until there is work this worker may take, or *timeout* passes.

    force_only: outside the scan window only force-scan jobs may run, so normal
    jobs must not wake the worker (they would just be re-queued in a loop).
    """
    def has_work() -> bool:
        return not _force_scan_queue.empty() or (not force_only and not _scan_queue.empty())

    async with _queue_cond:
        try:
            await asyncio.wait_for(_queue_cond.wait_for(has_work), timeout=timeout)
        except asyncio.TimeoutError:
            pass


async def _take_next_job() -> str | None:
    """Pop the next job without waiting, force-scan queue first; None if both are empty."""
    async with _state_lock:
        for queue, members, ordered in (
            (_force_scan_queue, _queued_force, _queued_force_ordered),
            (_scan_queue, _queued_normal, _queued_normal_ordered),
        ):
            try:
                plex_guid = queue.get_nowait()
            except asyncio.QueueEmpty:
                continue
            members.discard(plex_guid)
            if plex_guid in ordered:
                ordered.remove(plex_guid)
            return plex_guid
    return None


def is_paused() -> bool:
    return _paused


async def enqueue(plex_guid: str) -> None:
    # Hold _state_lock across the whole check-then-put to prevent two concurrent
    # callers from both passing the guard and inserting duplicates.
    async with _state_lock:
        if plex_guid in _current_guids:
            logger.debug("Skipping enqueue for %s: already scanning", plex_guid)
            return
        if plex_guid in _queued_force or plex_guid in _queued_normal:
            logger.debug("Skipping enqueue for %s: already queued", plex_guid)
            return
        await _scan_queue.put(plex_guid)
        _queued_normal.add(plex_guid)
        _queued_normal_ordered.append(plex_guid)
    await _notify_workers()


async def _enqueue_force(plex_guid: str) -> None:
    """Put a title on the force queue unless it is already scanning or queued there.

    Unlike force_scan_job() this leaves the DB flag alone (it is already set) and
    does not announce a new force request; it only restores in-memory queue state.
    """
    async with _state_lock:
        if plex_guid in _current_guids or plex_guid in _queued_force:
            return
        if plex_guid in _queued_normal:
            _queued_normal.discard(plex_guid)
            if plex_guid in _queued_normal_ordered:
                _queued_normal_ordered.remove(plex_guid)
        await _force_scan_queue.put(plex_guid)
        _queued_force.add(plex_guid)
        _queued_force_ordered.append(plex_guid)
    await _notify_workers()


async def enqueue_pending() -> None:
    """Drain the normal queue and re-enqueue all pending jobs in priority order.

    Safe to call at any time (e.g. on startup or via the reorder-queue API).
    Already-scanning titles (in _current_guids) are not affected.

    Order:
      1. Movies — most recently added first (descending integer rating_key).
      2. TV episodes — grouped by show_guid so all episodes of a show are
         contiguous; shows ordered by their highest rating_key descending
         (most recently added show first); episodes within each show
         ordered by rating_key ascending (episode order).

    Ignored titles are excluded; they remain pending in the DB so they can
    be un-ignored later without a fresh Plex sync.
    """
    # Drain the normal queue and clear tracking set so we can re-enqueue in order.
    # Force-queue and _current_guids are intentionally left untouched.
    async with _state_lock:
        while not _scan_queue.empty():
            try:
                _scan_queue.get_nowait()
            except Exception:
                break
        _queued_normal.clear()
        _queued_normal_ordered.clear()

    import json as _json
    excluded_library_ids = set(_json.loads(await db.get_setting("excluded_library_ids", "[]")))
    scan_ratings_set = set(_json.loads(await db.get_setting("scan_ratings", "[]")))

    jobs = await db.get_scan_jobs(status="pending")
    active = [j for j in jobs if is_scan_eligible(j, excluded_library_ids, scan_ratings_set)]

    # The force queue lives in memory, so a restart empties it. force_scan stays set in
    # the DB, and force jobs bypass eligibility, so restore every pending, non-ignored
    # one here; filtering them through is_scan_eligible dropped them silently (#106).
    forced = sorted(
        (j for j in jobs if j.get("force_scan") and not j.get("ignored")),
        key=lambda j: int(j.get("rating_key") or 0),
        reverse=True,
    )
    for job in forced:
        await _enqueue_force(job["plex_guid"])
    if forced:
        logger.info("Restored %d force-scan job(s) to the force queue", len(forced))

    movies = [j for j in active if j.get("media_type") != "episode"]
    episodes = [j for j in active if j.get("media_type") == "episode"]

    # Movies: newest Plex item first.
    movies.sort(key=lambda j: int(j.get("rating_key") or 0), reverse=True)

    # TV: group episodes by show_guid; fall back to the show name portion of
    # the title ("ShowName – Season – Episode") when show_guid is absent.
    from collections import defaultdict

    show_buckets: dict[str, list[dict]] = defaultdict(list)
    for ep in episodes:
        key = ep.get("show_guid") or ep.get("title", "").split(" – ")[0]
        show_buckets[key].append(ep)

    # Within each show sort by rating_key asc (episode order).
    for bucket in show_buckets.values():
        bucket.sort(key=lambda j: int(j.get("rating_key") or 0))

    # Order shows by the highest rating_key in each bucket desc
    # (most recently added show comes first).
    ordered_shows = sorted(
        show_buckets.values(),
        key=lambda eps: max(int(j.get("rating_key") or 0) for j in eps),
        reverse=True,
    )

    ordered = movies + [ep for eps in ordered_shows for ep in eps]

    enqueued = 0
    for job in ordered:
        await enqueue(job["plex_guid"])
        enqueued += 1
    if enqueued:
        logger.info(
            "Queued %d pending scan jobs (%d movies, %d episodes, %d ignored skipped)",
            enqueued,
            len(movies),
            len(episodes),
            len(jobs) - len(active),
        )


def _inference_resolution(model_name: str) -> int:
    """Return the square input size NudeNet uses for this model name (640m or 320n)."""
    return 640 if (model_name or "320n").strip().lower().startswith("640") else 320


def _get_detector(model_name: str = "320n", model_path: str = ""):
    detector = getattr(_thread_local, "nude_detector", None)
    detector_key = getattr(_thread_local, "nude_detector_key", None)

    # 640m is pre-warmed by scan_video() via _ensure_640m_model_async() before any
    # thread is dispatched. _get_detector() only does a local path check here.
    requested_resolution = _inference_resolution(model_name)
    selected_model_path = (model_path or "").strip()
    if requested_resolution == 640 and not selected_model_path:
        # Fallback: attempt sync check in case caller skipped pre-warm.
        candidate = MODELS_DIR / NUDENET_640_MODEL_FILENAME
        if _valid_model_file(candidate):
            selected_model_path = str(candidate)
        else:
            logger.warning("640m model not found locally; falling back to bundled 320n")
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
            try:
                detector = NudeDetector(**kwargs)
            except ImportError:
                raise
            except Exception as exc:
                if not selected_model_path:
                    raise
                # A model file that will not load must not silently turn every frame
                # into "nothing found" (#97): say so loudly and use the bundled 320n.
                logger.error(
                    "NudeNet could not load %s (%s); falling back to bundled 320n",
                    selected_model_path, exc,
                )
                current_key = (320, "")
                detector = NudeDetector(inference_resolution=320)
            ort_runtime.limit_nudenet_threads(detector, current_key[1])
            _thread_local.nude_detector = detector
            _thread_local.nude_detector_key = current_key
            logger.info("NudeNet detector loaded in thread %s", threading.get_ident())
        except ImportError:
            logger.error("nudenet package not installed. Run: pip install nudenet")
            raise
    return detector


def _ensure_local_640m_model() -> str:
    """Blocking download of the 640m model. Must be called via asyncio.to_thread so the
    event loop is not blocked; caller must hold _model_download_lock before dispatching.

    Returns the model path, or "" if no valid model could be obtained. An existing
    invalid file (HTML page, truncated download) is deleted and fetched again.
    """
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    target = MODELS_DIR / NUDENET_640_MODEL_FILENAME
    if _valid_model_file(target):
        return str(target)
    if target.exists():
        logger.warning("Discarding invalid NudeNet 640m model file %s (%d bytes)", target, target.stat().st_size)
        target.unlink(missing_ok=True)

    tmp = target.with_suffix(target.suffix + ".part")
    for url in NUDENET_640_DOWNLOAD_URLS:
        try:
            logger.info("Downloading NudeNet 640m model from %s", url)
            with httpx.Client(timeout=120.0, follow_redirects=True) as client:
                with client.stream("GET", url, headers={"Accept": "application/octet-stream"}) as resp:
                    resp.raise_for_status()
                    if resp.headers.get("content-type", "").startswith("text/"):
                        raise ValueError(f"got {resp.headers['content-type']} instead of a model file")
                    with open(tmp, "wb") as out:
                        for chunk in resp.iter_bytes():
                            if chunk:
                                out.write(chunk)
            if not _valid_model_file(tmp):
                raise ValueError(f"downloaded file is not a valid model ({tmp.stat().st_size} bytes)")
            tmp.replace(target)
            logger.info("Downloaded NudeNet 640m model to %s", target)
            return str(target)
        except Exception as exc:
            logger.warning("Failed to download NudeNet 640m model from %s: %s", url, exc)
            tmp.unlink(missing_ok=True)
    return ""


async def _ensure_640m_model_async() -> str:
    """Ensure the 640m model file is present, downloading it if needed.

    Uses asyncio.Lock so only one coroutine runs the download at a time; others
    wait and then return the cached path without redundant downloads.
    """
    target = MODELS_DIR / NUDENET_640_MODEL_FILENAME
    # Fast path: model already on disk — no lock needed.
    if _valid_model_file(target):
        return str(target)

    async with _model_download_lock:
        # Re-check after acquiring lock: another coroutine may have just finished.
        if _valid_model_file(target):
            return str(target)
        return await asyncio.to_thread(_ensure_local_640m_model)


_logged_classify_errors: set[str] = set()


def _classify_frame(
    jpeg_bytes: bytes,
    threshold: float,
    enabled_labels: set[str],
    model_name: str,
    model_path: str,
    label_thresholds: dict[str, float] | None = None,
) -> tuple[bool, float, list[str]]:
    """Return (is_nude, confidence, detected_labels) for a JPEG frame.

    A detection counts only if its label is enabled and its score reaches that
    label's entry in label_thresholds, else the global threshold (#120).
    detected_labels holds just the labels that counted; confidence is the best
    score among them, or among all enabled labels when none counted.
    """
    overrides = label_thresholds or {}
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

        best_any = 0.0
        best_passing = 0.0
        detected = []

        for det in results:
            label = det.get("class")
            score = det.get("score", 0.0)
            if label not in enabled_labels:
                continue
            best_any = max(best_any, score)
            if score < overrides.get(label, threshold):
                continue
            if label not in detected:
                detected.append(label)
            best_passing = max(best_passing, score)

        if detected:
            return True, best_passing, detected
        return False, best_any, []
    except Exception as exc:
        # The first occurrence of each distinct error is a warning: a failure that
        # repeats on every frame would otherwise look like a title with no nudity.
        message = str(exc)
        if message not in _logged_classify_errors:
            _logged_classify_errors.add(message)
            logger.warning("NudeNet classification failed: %s", message)
        else:
            logger.debug("Classification error: %s", message)
        return False, 0.0, []


async def _credits_start_ms(rating_key: str) -> int | None:
    """Return where the end credits start per Plex, or None if unknown.

    Prefers a marker Plex flags as final; failing that, the latest credits marker,
    since an earlier one may be a mid-film credit roll. Any Plex error means "no
    marker": the scan must not fail because credits could not be located (#121).
    """
    try:
        markers = await plex_mod.get_client().get_markers(rating_key)
    except Exception as exc:
        logger.debug("No credits marker for rating_key=%s: %s", rating_key, exc)
        return None
    credits = [m for m in markers if m.get("marker_type") == "credits"]
    final = [m for m in credits if m.get("final")]
    chosen = final or credits
    if not chosen:
        return None
    return max(int(m["start_ms"]) for m in chosen)


def _segment_source(nudenet_hits: int, hit_count: int, max_siglip_run: int,
                    min_hits: int, siglip_min_hits: int) -> str | None:
    """Decide what a finished cluster becomes: "scanner", "siglip", or nothing.

    Any NudeNet hit makes it a normal scanner segment, judged by segment_min_hits
    over all its frames as before. A cluster built only from SigLIP fallback hits
    needs a run of siglip_min_hits consecutive frames, since single-frame SigLIP
    hits are noisy.
    """
    if nudenet_hits > 0:
        return "scanner" if hit_count >= min_hits else None
    return "siglip" if max_siglip_run >= siglip_min_hits else None


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

    # force_scan overrides every gate below — the user explicitly requested this scan.
    is_force_scan = bool(job.get("force_scan", 0))

    if not is_force_scan:
        # Check if the title's library is excluded from scanning.
        import json as _json
        excluded_library_ids = set(_json.loads(await db.get_setting("excluded_library_ids", "[]")))
        if job.get("library_id") in excluded_library_ids:
            logger.info(
                "Skipping %s: library '%s' is excluded from scanning",
                job["title"],
                job.get("library_id"),
            )
            return

        # Check if title is marked as ignored
        if bool(job.get("ignored", 0)):
            logger.info("Skipping ignored title: %s", job["title"])
            return

        # Safety-net gate: enqueue_pending() and scan_library() now pre-filter using
        # is_scan_eligible(), but this check catches anything that slips through
        # (e.g. a single-title enqueue from the API or MCP before the config was set).
        scan_ratings: set[str] = set(config.scan_ratings) if config.scan_ratings else set()
        if scan_ratings:
            job_rating = (job.get("content_rating") or "").strip()
            if job_rating not in scan_ratings:
                logger.info(
                    "Skipping %s: content rating '%s' not in scan_ratings %s",
                    job["title"],
                    job_rating,
                    scan_ratings,
                )
                return

        # Don't scan outside the configured time window unless force-scanned.
        if not config.is_scan_window():
            logger.warning("Attempted to scan outside scan window (not force-scanned): %s. Re-queueing.", job["title"])
            await db.update_scan_job_status(plex_guid, "pending")
            await enqueue(plex_guid)
            return

    file_path = job["file_path"]
    title = job["title"]
    rating_key = str(job.get("rating_key") or "")

    # Parse multi-part file list; fall back to single-file if not set.
    import json as _json_mod
    _raw_parts = job.get("part_files") or "[]"
    try:
        all_parts = _json_mod.loads(_raw_parts)
    except Exception:
        all_parts = []
    if not all_parts:
        all_parts = [file_path]

    for p in all_parts:
        if not os.path.isfile(p):
            logger.error("Video file not found: %s", p)
            await db.update_scan_job_status(plex_guid, "failed", error_msg=f"File not found: {p}")
            return

    async with _state_lock:
        _current_guids.add(plex_guid)
    # Fresh scan run: clear prior segments so retries/re-scans do not duplicate entries.
    deleted = await db.delete_segments_for_guid(plex_guid)
    if deleted:
        logger.info("Cleared %d previous segment(s) for %s", deleted, title)
        events.publish("segments_changed", guid=plex_guid)
    await db.update_scan_job_status(plex_guid, "scanning", progress=0.0)
    logger.info("Scanning: %s (%d part(s))", title, len(all_parts))
    events.publish("scan_started", guid=plex_guid)
    # Progress events are throttled by wall time, not frames: frame rate varies with
    # the file and model, and the UI only needs a steady bar.
    last_progress_event = 0.0

    try:
        # Smaller interval improves recall for short scenes; configurable in settings.
        step_ms = max(1000, int(getattr(config, "scan_step_ms", 5000)))
        gap_ms = max(1000, int(getattr(config, "segment_gap_ms", 12000)))
        min_hits = max(1, int(getattr(config, "segment_min_hits", 1)))

        # Get duration for each part; total determines progress denominator.
        part_durations_ms: list[int] = []
        for p in all_parts:
            d = await get_duration_ms(p)
            if not d:
                raise RuntimeError(f"Could not determine video duration for: {p}")
            part_durations_ms.append(d)

        total_duration_ms = sum(part_durations_ms)
        total_steps = max(1, total_duration_ms // step_ms)
        segments_inserted = 0

        cluster_start_ms: int | None = None
        cluster_prev_ms: int = 0
        cluster_hit_count = 0
        cluster_best_jpeg: bytes = b""
        cluster_best_score = 0.0
        cluster_best_ms: int | None = None  # absolute time of the frame kept as the thumbnail
        cluster_detected_labels: list[str] = []
        cluster_nudenet_hits = 0
        cluster_max_siglip_run = 0
        siglip_run = 0  # consecutive SigLIP-only hit frames ending at the current frame

        threshold = config.confidence_threshold
        enabled_labels = set(config.scan_labels) if config.scan_labels else set()
        nudenet_model = str(getattr(config, "nudenet_model", "320n"))
        nudenet_model_path = str(getattr(config, "nudenet_model_path", ""))
        # A user-supplied model file; checked before the 640m download fills the path.
        custom_model = bool(nudenet_model_path)

        # Pre-warm 640m model under asyncio.Lock before dispatching threads so
        # parallel workers do not race to download the same file concurrently.
        if nudenet_model.startswith("640") and not nudenet_model_path:
            nudenet_model_path = await _ensure_640m_model_async()
            if not nudenet_model_path:
                logger.warning("Could not prepare 640m model for %s; falling back to 320n", title)
                nudenet_model = "320n"

        siglip_on = bool(getattr(config, "siglip_enabled", False)) and bool(enabled_labels)
        siglip_min_hits = max(1, int(getattr(config, "siglip_min_hits", 2)))
        if siglip_on and not await siglip_detector.ensure_models_async():
            logger.warning("SigLIP models unavailable; scanning %s with NudeNet only", title)
            siglip_on = False

        # Frames from here on are credits: SigLIP reads text-heavy screens as nudity,
        # so it skips them while NudeNet keeps looking (a post-credits scene counts).
        credits_start_ms: int | None = None
        if siglip_on and rating_key and bool(getattr(config, "ignore_credits", True)):
            credits_start_ms = await _credits_start_ms(rating_key)
            if credits_start_ms is not None:
                logger.info("Skipping SigLIP after credits start at %dms for %s", credits_start_ms, title)

        # Frames are sized for NudeNet. SigLIP shrinks every frame to 224 px, so a
        # 320 px frame loses it nothing, but it must never get less than that.
        frame_size = _inference_resolution(nudenet_model)
        if siglip_on:
            frame_size = max(frame_size, siglip_detector.IMAGE_SIZE)

        # Recorded on each segment so the UI can say which model found it. Taken
        # after the 640m fallback above, so it names the model actually used.
        nudenet_detector = f"nudenet-{nudenet_model}" + ("-custom" if custom_model else "")

        THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)

        # The part being scanned, for re-sampling a flagged window inside it (#133).
        part_file_now: str | None = None
        part_offset_now = 0
        part_duration_now = 0
        hardware = bool(getattr(config, "hw_decode", True))
        refine_step_ms = max(0, int(getattr(config, "refine_step_ms", 500)))

        async def _judge(jpeg: bytes, at_ms: int, use_siglip: bool = True) -> tuple[bool, bool, float, list[str]]:
            """Return (is_nude, siglip_hit, score, labels) for one frame at absolute time at_ms.

            SigLIP only looks at frames NudeNet passed over: its job is the scenes NudeNet
            misses, not a second opinion on its hits. asyncio runs inference on the pool, so
            ffmpeg's pipe keeps filling while a frame is being scored.
            """
            is_nude, score, labels = await _infer(
                _classify_frame, jpeg, threshold, enabled_labels, nudenet_model, nudenet_model_path,
                config.label_thresholds,
            )
            siglip_hit = False
            in_credits = credits_start_ms is not None and at_ms >= credits_start_ms
            if not is_nude and use_siglip and siglip_on and not in_credits:
                siglip_score, siglip_label = await _infer(siglip_detector.score_frame, jpeg, enabled_labels)
                siglip_hit = siglip_score >= config.siglip_threshold_for(siglip_label)
                if siglip_hit:
                    # Ranked below any NudeNet score so a NudeNet frame always wins the
                    # thumbnail and labels of a mixed cluster.
                    score, labels = siglip_score - 1.0, [siglip_label]
            return is_nude, siglip_hit, score, labels

        async def _refine_cluster(source: str) -> str:
            """Re-sample the open cluster's window densely; keep its peak frame and widen its bounds.

            Sampling every few seconds means the best sampled frame is often the edge of a
            scene, and a scene can start or run on between samples (#133). This looks at the
            window at refine_step_ms, takes the highest-scoring frame as the thumbnail, and
            moves the start earlier / the last flagged time later to cover frames above
            threshold. Bounds only ever grow, and the segment is kept or dropped on the coarse
            result as before. Best effort: any failure leaves the coarse result.

            Returns the segment's source. A SigLIP-only cluster whose window turns out to hold
            NudeNet hits becomes a scanner segment (if the usual hit count allows it), since
            NudeNet now vouches for it; otherwise those hits are ignored so the cluster's
            scores stay in one unit (SigLIP scores are stored offset by -1).
            """
            nonlocal cluster_start_ms, cluster_prev_ms, cluster_best_jpeg, cluster_best_score
            nonlocal cluster_detected_labels, cluster_best_ms, cluster_nudenet_hits, cluster_hit_count
            if part_file_now is None or cluster_start_ms is None:
                return source
            part_end = part_offset_now + part_duration_now
            # A scanner segment (NudeNet hit) is refined by NudeNet alone: SigLIP's noisier
            # hits must not stretch a segment NudeNet already vouches for.
            use_siglip = source == "siglip"

            async def scan(lo_ms: int, hi_ms: int, dstep: int) -> list[tuple[int, bool, float, list[str], bytes]]:
                found = []
                try:
                    async for rel_ms, jpeg in extract_frames_window(
                        part_file_now, lo_ms - part_offset_now, hi_ms - lo_ms, dstep, frame_size, hardware
                    ):
                        if not jpeg:
                            continue
                        at_ms = part_offset_now + rel_ms
                        is_nude, siglip_hit, score, labels = await _judge(jpeg, at_ms, use_siglip)
                        if is_nude or siglip_hit:
                            found.append((at_ms, is_nude, score, labels, jpeg))
                except Exception as exc:
                    logger.warning("Re-sampling %d-%dms of '%s' failed, keeping the coarse result: %s",
                                   lo_ms, hi_ms, title, exc)
                return found

            lo = max(part_offset_now, cluster_start_ms - step_ms)
            hi = min(part_end, cluster_prev_ms + step_ms)
            dstep = max(refine_step_ms, -(-(hi - lo) // REFINE_MAX_FRAMES))
            hits = await scan(lo, hi, dstep)
            # A hit at the window's edge means the scene runs on: keep looking, up to one merge
            # gap each way, in chunks of two coarse steps.
            chunk = max(2 * step_ms, 4 * dstep)
            grown = 0
            while hits and grown < gap_ms and hi < part_end and max(h[0] for h in hits) >= hi - 2 * dstep:
                new_hi = min(part_end, hi + chunk)
                more = await scan(hi, new_hi, dstep)
                hits += more
                grown += new_hi - hi
                hi = new_hi
                if not more:
                    break
            grown = 0
            while hits and grown < gap_ms and lo > part_offset_now and min(h[0] for h in hits) <= lo + 2 * dstep:
                new_lo = max(part_offset_now, lo - chunk)
                more = await scan(new_lo, lo, dstep)
                hits += more
                grown += lo - new_lo
                lo = new_lo
                if not more:
                    break

            if source == "siglip":
                dense_nudenet = sum(1 for h in hits if h[1])
                if dense_nudenet and _segment_source(
                    cluster_nudenet_hits + dense_nudenet, cluster_hit_count + dense_nudenet,
                    cluster_max_siglip_run, min_hits, siglip_min_hits,
                ) == "scanner":
                    cluster_nudenet_hits += dense_nudenet
                    cluster_hit_count += dense_nudenet
                    source = "scanner"
                else:
                    hits = [h for h in hits if not h[1]]
            for at_ms, is_nude, score, labels, jpeg in sorted(hits, key=lambda h: h[0]):
                if score > cluster_best_score:
                    cluster_best_jpeg, cluster_best_score = jpeg, score
                    cluster_detected_labels, cluster_best_ms = labels.copy(), at_ms
                elif is_nude:
                    for label in labels:
                        if label not in cluster_detected_labels:
                            cluster_detected_labels.append(label)
                cluster_start_ms = min(cluster_start_ms, at_ms)
                cluster_prev_ms = max(cluster_prev_ms, at_ms)
            return source

        async def _flush_cluster() -> None:
            nonlocal cluster_start_ms, cluster_prev_ms, cluster_hit_count
            nonlocal cluster_best_jpeg, cluster_best_score, cluster_detected_labels, segments_inserted
            nonlocal cluster_nudenet_hits, cluster_max_siglip_run, cluster_best_ms

            if cluster_start_ms is None:
                return

            source = _segment_source(
                cluster_nudenet_hits, cluster_hit_count, cluster_max_siglip_run, min_hits, siglip_min_hits
            )
            if source:
                if refine_step_ms > 0:
                    source = await _refine_cluster(source)
                end_ms = cluster_prev_ms + gap_ms
                thumb_path = ""
                if cluster_best_jpeg:
                    # Strip all characters invalid on Windows (colons, asterisks, etc.)
                    # plex_guid values like "imdb://tt123" would otherwise produce "imdb:__tt123"
                    # which raises [Errno 22] on Windows when used as a filename.
                    safe_guid = re.sub(r"[^\w\-]", "_", plex_guid)
                    thumb_filename = f"{safe_guid}_{cluster_start_ms}.jpg"
                    thumb_full = THUMBNAILS_DIR / thumb_filename
                    thumb_full.write_bytes(cluster_best_jpeg)
                    thumb_path = str(thumb_full)

                labels_str = ",".join(cluster_detected_labels)
                await db.insert_segment(
                    plex_guid=plex_guid,
                    title=title,
                    start_ms=cluster_start_ms,
                    end_ms=end_ms,
                    # Undo the -1 ranking offset applied to SigLIP scores below.
                    confidence=cluster_best_score if source == "scanner" else cluster_best_score + 1.0,
                    thumbnail_path=thumb_path,
                    labels=labels_str,
                    # Severity follows what was detected, so a viewer's level decides
                    # exposed vs covered vs genitalia (#126). SigLIP-only segments say
                    # where they came from; users can tell them apart and threshold them.
                    severity=severity_for_labels(labels_str),
                    source=source,
                    detector=nudenet_detector if source == "scanner" else siglip_detector.DETECTOR_ID,
                    thumbnail_ms=cluster_best_ms,
                )
                segments_inserted += 1
                events.publish("segments_changed", guid=plex_guid)

            cluster_start_ms = None
            cluster_prev_ms = 0
            cluster_hit_count = 0
            cluster_best_jpeg = b""
            cluster_best_score = 0.0
            cluster_best_ms = None
            cluster_detected_labels = []
            cluster_nudenet_hits = 0
            cluster_max_siglip_run = 0

        # Scan each part in sequence. For multi-part movies (CD1/CD2) all parts
        # share the same plex_guid; frames are assigned absolute timestamps by
        # adding part_time_offset_ms so segments span the full concatenated runtime.
        # Plex reports viewOffset as an absolute position across all parts, so the
        # filter engine requires no changes to match against these segments.
        part_time_offset_ms = 0
        for part_idx, (part_file, part_duration_ms) in enumerate(zip(all_parts, part_durations_ms)):
            if len(all_parts) > 1:
                logger.info(
                    "Scanning part %d/%d for '%s': %s (offset=%dms)",
                    part_idx + 1, len(all_parts), title, part_file, part_time_offset_ms,
                )

            part_file_now, part_offset_now, part_duration_now = part_file, part_time_offset_ms, part_duration_ms
            async for frame_offset_ms, jpeg in extract_frames_batch(
                part_file, step_ms, part_duration_ms, frame_size,
                hardware=bool(getattr(config, "hw_decode", True)),
            ):
                # Absolute timestamp across all parts.
                offset_ms = part_time_offset_ms + frame_offset_ms
                idx = offset_ms // step_ms

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

                if jpeg:
                    is_nude, siglip_hit, score, detected_labels = await _judge(jpeg, offset_ms)
                    siglip_run = siglip_run + 1 if siglip_hit else 0

                    if is_nude or siglip_hit:
                        if cluster_start_ms is not None and offset_ms - cluster_prev_ms > gap_ms:
                            # Previous segment is complete; persist it immediately.
                            await _flush_cluster()
                        if cluster_start_ms is None:
                            cluster_start_ms = offset_ms
                            cluster_hit_count = 0
                            cluster_best_jpeg = jpeg
                            cluster_best_score = score
                            cluster_best_ms = offset_ms
                            cluster_detected_labels = detected_labels.copy()
                        elif score > cluster_best_score:
                            cluster_best_jpeg = jpeg
                            cluster_best_score = score
                            cluster_best_ms = offset_ms
                            cluster_detected_labels = detected_labels.copy()
                        elif is_nude:
                            # Merge labels from lower-confidence detections
                            for label in detected_labels:
                                if label not in cluster_detected_labels:
                                    cluster_detected_labels.append(label)
                        cluster_prev_ms = offset_ms
                        cluster_hit_count += 1
                        cluster_nudenet_hits += int(is_nude)
                        cluster_max_siglip_run = max(cluster_max_siglip_run, siglip_run)

                progress = (idx + 1) / total_steps
                if idx % 30 == 0:  # Update DB every ~2.5 min of video at 5 s steps
                    await db.update_scan_job_status(plex_guid, "scanning", progress=progress)
                now = time.monotonic()
                if now - last_progress_event >= PROGRESS_EVENT_INTERVAL_S:
                    last_progress_event = now
                    events.publish("scan_progress", guid=plex_guid, progress=round(progress, 4))

            # Flush any open cluster before the next part starts; the time gap
            # between parts would prevent natural flushing via the gap_ms check.
            await _flush_cluster()
            part_time_offset_ms += part_duration_ms

        await db.update_scan_job_status(plex_guid, "done", progress=1.0)

        if rating_key:
            try:
                client = plex_mod.get_client()
                await client.update_cleanplex_summary(
                    rating_key=rating_key,
                    status="Scanned",
                    segment_count=segments_inserted,
                )
            except Exception as exc:
                logger.debug("Could not update Plex summary metadata for %s: %s", plex_guid, exc)

        logger.info("Scan complete: %s — found %d segment(s)", title, segments_inserted)

    except Exception as exc:
        logger.error("Scan failed for %s: %s", title, exc)
        await db.update_scan_job_status(plex_guid, "failed", error_msg=str(exc))
    finally:
        async with _state_lock:
            _current_guids.discard(plex_guid)
        # Covers every exit (done, failed, skipped, paused, window ended): the UI
        # refetches the job to learn which.
        events.publish("scan_finished", guid=plex_guid)


async def _scanner_worker_loop(worker_id: int, get_config_fn) -> None:
    """Single scanner worker — scans queued jobs and respects pause/scan window."""
    while True:
        config = await get_config_fn()

        plex_guid = await _take_next_job()
        if plex_guid is None:
            # Queue is empty, respect scan window
            in_window = config.is_scan_window()
            if not in_window:
                if not _paused:
                    pause_scanner()
            else:
                if _paused:
                    resume_scanner()
            # The timeout only bounds how long the window check above goes stale;
            # new work wakes the worker immediately.
            await _wait_for_work(force_only=not in_window, timeout=30)
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
            # Only a force-scan job can run now; sleep until one arrives.
            await _wait_for_work(force_only=True, timeout=60)
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

    while True:
        # Re-apply queue ordering on every start/restart so settings changes
        # (or an explicit reorder-queue API call) take effect immediately.
        await enqueue_pending()

        config = await get_config_fn()
        worker_count = max(1, int(getattr(config, "scan_workers", 2)))
        _worker_pool_size = worker_count
        threads = ort_runtime.configure(worker_count, int(getattr(config, "inference_threads", 0)))
        logger.info("Inference sessions capped at %d thread(s) each", threads)
        _restart_requested = False
        logger.info("Starting scanner pool with %d worker(s)", worker_count)
        _replace_inference_pool(worker_count)

        # Create worker tasks
        worker_tasks = [
            asyncio.create_task(_scanner_worker_loop(i + 1, get_config_fn))
            for i in range(worker_count)
        ]

        try:
            # Run workers until restart or unexpected completion.
            # timeout=5 ensures _restart_requested is polled promptly even while
            # all workers are mid-scan (workers never complete normally, so without
            # a timeout asyncio.wait would block indefinitely).
            while not _restart_requested:
                done, _ = await asyncio.wait(
                    worker_tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=5.0,
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
