"""Background job that re-samples segments stored before dense sampling existed (#141).

Each segment's window is looked at again at refine_step_ms (and only that window, never the
whole film). The peak frame becomes the thumbnail when it scores at least as high as the
stored one, the bounds grow to cover frames above threshold, and the segment is marked refined
so it is processed once. Runs inside the scan window, one segment at a time.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from . import database as db
from . import events, hover_previews, plex_summary, scanner
from .config import Config
from .frame_extractor import extract_frames_window, get_duration_ms
from .importers._common import severity_for_labels
from .logger import get_logger
from .segment_refine import dense_hits

logger = get_logger(__name__)

# How often to check whether the window has opened or new work has appeared.
CHECK_INTERVAL_S = 300
BATCH_SIZE = 20
# A pause between segments so a long backlog never monopolises the machine.
PAUSE_S = 0.2


def _single_file(part_files: str) -> bool:
    """True unless the title is stored in several parts (their offsets are not worked out here)."""
    try:
        return len(json.loads(part_files or "[]")) <= 1
    except ValueError:
        return True


def _thumbnail_target(seg: dict) -> Path:
    if seg.get("thumbnail_path"):
        return Path(seg["thumbnail_path"])
    safe_guid = re.sub(r"[^\w\-]", "_", seg["plex_guid"])
    return scanner.THUMBNAILS_DIR / f"{safe_guid}_{seg['start_ms']}.jpg"


async def _write_thumbnail(path: Path, jpeg: bytes) -> None:
    def write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(path.suffix + ".part")
        part.write_bytes(jpeg)
        os.replace(part, path)  # atomic: a reader never sees half a file

    await asyncio.to_thread(write)


async def refine_segment(
    seg: dict,
    config: Config,
    detectors: tuple[str, str, bool, bool],
    durations: dict[str, int],
    credits: dict[str, int | None],
) -> str:
    """Refine one stored segment; return "refined", "unchanged", "skipped" or "offline".

    "offline" is the only outcome that leaves the segment for a later run (the file is not
    reachable now). Anything else marks it refined.
    """
    path = seg.get("file_path") or ""
    if not _single_file(seg.get("part_files") or ""):
        await db.mark_segment_refined(seg["id"])
        return "skipped"
    if not path or not await asyncio.to_thread(os.path.isfile, path):
        return "offline"

    nudenet_model, nudenet_model_path, custom_model, siglip_on = detectors
    if path not in durations:
        durations[path] = await get_duration_ms(path) or 0
    duration = durations[path]
    if not duration:
        await db.mark_segment_refined(seg["id"])
        return "skipped"

    from_siglip = seg["source"] == "siglip"
    credits_start = None
    if from_siglip and siglip_on and config.ignore_credits and seg.get("rating_key"):
        if seg["rating_key"] not in credits:
            credits[seg["rating_key"]] = await scanner._credits_start_ms(str(seg["rating_key"]))
        credits_start = credits[seg["rating_key"]]
    judge = scanner.make_judge(config, nudenet_model, nudenet_model_path, siglip_on, credits_start)

    async def judge_for_source(jpeg: bytes, at_ms: int):
        # A scanner segment is refined by NudeNet alone; a SigLIP one by both.
        return await judge(jpeg, at_ms, from_siglip)

    gap = config.segment_gap_ms
    hits = await dense_hits(
        part_file=path, part_offset_ms=0, part_end_ms=duration,
        start_ms=seg["start_ms"], last_ms=max(seg["start_ms"], seg["end_ms"] - gap),
        coarse_step_ms=config.scan_step_ms, gap_ms=gap, refine_step_ms=config.refine_step_ms,
        frame_size=scanner.frame_size_for(nudenet_model, siglip_on),
        hardware=bool(config.hw_decode), judge=judge_for_source, extract=extract_frames_window,
        label=seg.get("title") or seg["plex_guid"],
    )
    if not hits:
        await db.mark_segment_refined(seg["id"])
        return "unchanged"

    source, detector = seg["source"], seg["detector"] or ""
    nudenet_hits = [h for h in hits if h[1]]
    if from_siglip and nudenet_hits:
        # NudeNet now vouches for the scene: it becomes a scanner segment, scored by NudeNet.
        source = "scanner"
        detector = f"nudenet-{nudenet_model}" + ("-custom" if custom_model else "")
        ranked = nudenet_hits
        best_score = max(h[2] for h in ranked)
        old_score = 0.0  # a SigLIP score is not comparable, so the NudeNet frame always wins
    elif from_siglip:
        ranked = hits
        best_score = max(h[2] for h in ranked) + 1.0  # stored SigLIP scores are un-offset
        old_score = seg["confidence"]
    else:
        ranked = hits
        best_score = max(h[2] for h in ranked)
        old_score = seg["confidence"]

    best = max(ranked, key=lambda h: h[2])
    replace_thumbnail = best_score >= old_score
    if replace_thumbnail:
        await _write_thumbnail(_thumbnail_target(seg), best[4])

    labels = [lbl.strip() for lbl in (seg["labels"] or "").split(",") if lbl.strip()]
    for h in ranked:
        for lbl in h[3]:
            if lbl not in labels:
                labels.append(lbl)
    labels_text = ",".join(labels)
    times = [h[0] for h in hits]
    start_ms = min(seg["start_ms"], min(times))
    end_ms = max(seg["end_ms"], max(times) + gap)
    bounds_changed = (start_ms, end_ms) != (seg["start_ms"], seg["end_ms"])

    await db.apply_segment_refinement(
        seg["id"],
        start_ms=start_ms, end_ms=end_ms,
        confidence=max(seg["confidence"], best_score) if source == seg["source"] else best_score,
        labels=labels_text, severity=severity_for_labels(labels_text, seg["severity"]),
        source=source, detector=detector,
        thumbnail_ms=best[0] if replace_thumbnail else None,
        reset_hover=bounds_changed,
    )
    if bounds_changed:
        hover_previews.hover_path(seg["id"]).unlink(missing_ok=True)
    events.publish("segments_changed", guid=seg["plex_guid"])
    plex_summary.refresh_soon(seg["plex_guid"])
    return "refined"


async def refine_pending(
    get_config_fn: Callable[[], Awaitable[Config]], should_continue: Callable[[], Awaitable[bool]]
) -> int:
    """Refine waiting segments while *should_continue* holds; return how many were looked at."""
    config = await get_config_fn()
    detectors = await scanner.prepare_detectors(config, "segment refinement")
    durations: dict[str, int] = {}
    credits: dict[str, int | None] = {}
    seen: set[int] = set()
    looked_at = 0
    while await should_continue():
        batch = [seg for seg in await db.get_segments_to_refine(BATCH_SIZE) if seg["id"] not in seen]
        if not batch:
            break
        for seg in batch:
            if not await should_continue():
                return looked_at
            seen.add(seg["id"])  # offline segments stay pending; this run must not loop on them
            try:
                await refine_segment(seg, config, detectors, durations, credits)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Could not refine segment %s: %s", seg["id"], exc)
            looked_at += 1
            await asyncio.sleep(PAUSE_S)
    return looked_at


async def thumbnail_backfill_loop(get_config_fn: Callable[[], Awaitable[Config]]) -> None:
    """Run refine_pending whenever the scan window is open, refinement is on and segments wait."""

    async def active() -> bool:
        config = await get_config_fn()
        return config.refine_step_ms > 0 and config.is_scan_window()

    while True:
        try:
            if await active() and await db.count_segments_to_refine():
                n = await refine_pending(get_config_fn, active)
                if n:
                    logger.info("Segment refinement: looked at %d segment(s), %d left", n,
                                await db.count_segments_to_refine())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Segment refinement run failed")
        await asyncio.sleep(CHECK_INTERVAL_S)
