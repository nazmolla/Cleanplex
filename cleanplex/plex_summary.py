"""Describes a title's scan in its Plex summary and keeps that block current (#136)."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timezone

import cleanplex.plex_client as plex_mod
from . import database as db
from .logger import get_logger

logger = get_logger(__name__)

# Coalesces a burst of edits (bulk deletes, a run of false-positive marks) into one write.
DEBOUNCE_S = 2.0
_pending: dict[str, asyncio.Task] = {}

_SOURCE_NAMES = {"scanner": "NudeNet", "siglip": "SigLIP", "subtitles": "subtitles"}


def label_name(label: str) -> str:
    """FEMALE_BREAST_EXPOSED -> "exposed female breast"."""
    *noun, state = label.lower().split("_")
    return f"{state} {' '.join(noun)}" if noun else label.lower()


def local_time(stamp: str) -> str:
    """A SQLite UTC timestamp as local "YYYY-MM-DD HH:MM", the clock the server's owner reads."""
    try:
        parsed = datetime.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return stamp[:16]
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M")


def duration_text(ms: int) -> str:
    """4m 12s, 1h 28m; seconds are dropped once the span is over an hour."""
    total = max(0, round(ms / 1000))
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


def _covered_ms(segments: list[dict]) -> int:
    """Total time the segments cover, counting overlaps once (segments are sorted by start)."""
    covered = 0
    cursor = 0
    for seg in segments:
        start = max(seg["start_ms"], cursor)
        if seg["end_ms"] > start:
            covered += seg["end_ms"] - start
            cursor = seg["end_ms"]
    return covered


def describe(stats: dict) -> list[str]:
    """The lines of the Cleanplex block for one title, from db.get_summary_stats."""
    job, segments = stats["job"], stats["segments"]
    status = job["status"]
    if status == "failed":
        return ["Cleanplex scan: failed", *_review_lines(stats)]
    if status != "done":
        return ["Cleanplex scan: pending", *_review_lines(stats)]

    scanned = f"scanned {local_time(job['finished_at'])}" if job.get("finished_at") else "scanned"
    if not segments:
        return [f"Cleanplex scan: nothing found ({scanned})", *_review_lines(stats)]

    n = len(segments)
    lines = [f"Cleanplex scan: {n} segment{'s' if n != 1 else ''} found ({scanned})"]
    covered = _covered_ms(segments)
    runtime = int(job.get("duration_ms") or 0)
    share = f", {round(100 * covered / runtime)}% of the runtime ({duration_text(runtime)})" if runtime else ""
    lines.append(f"Flagged time: {duration_text(covered)}{share}")

    labels = Counter(
        label for seg in segments for label in dict.fromkeys(t.strip() for t in (seg["labels"] or "").split(",") if t.strip())
    )
    if labels:
        lines.append("Found: " + ", ".join(f"{label_name(lab)} ×{c}" for lab, c in labels.most_common()))
    severities = Counter(seg["severity"] or "high" for seg in segments)
    lines.append("Severity: " + ", ".join(f"{severities[s]} {s}" for s in ("high", "medium", "low") if severities[s]))
    sources = Counter(_SOURCE_NAMES.get(seg["source"] or "scanner", "imported") for seg in segments)
    lines.append("From: " + ", ".join(f"{name} {c}" for name, c in sources.most_common()))
    lines.extend(_review_lines(stats))
    return lines


def _review_lines(stats: dict) -> list[str]:
    parts = []
    if stats["false_positives"]:
        n = stats["false_positives"]
        parts.append(f"{n} marked as false positive{'s' if n != 1 else ''}")
    if stats["relabeled"]:
        n = stats["relabeled"]
        parts.append(f"{n} label{'s' if n != 1 else ''} corrected")
    return [f"Reviewed: {', '.join(parts)}"] if parts else []


async def refresh_now(plex_guid: str) -> None:
    """Write the title's current description to Plex. Quiet on failure: this is a courtesy."""
    try:
        stats = await db.get_summary_stats(plex_guid)
        if stats is None or not stats["job"].get("rating_key"):
            return
        await plex_mod.get_client().update_cleanplex_summary(
            rating_key=str(stats["job"]["rating_key"]),
            status=stats["job"]["status"],
            segment_count=len(stats["segments"]),
            details=describe(stats),
        )
    except Exception as exc:
        logger.debug("Could not refresh Plex summary for guid=%s: %s", plex_guid, exc)


async def _refresh_after_delay(plex_guid: str) -> None:
    await asyncio.sleep(DEBOUNCE_S)
    _pending.pop(plex_guid, None)
    await refresh_now(plex_guid)


def refresh_soon(plex_guid: str) -> None:
    """Schedule a debounced refresh; a newer call for the same title replaces a waiting one."""
    existing = _pending.pop(plex_guid, None)
    if existing and not existing.done():
        existing.cancel()
    _pending[plex_guid] = asyncio.create_task(_refresh_after_delay(plex_guid))
