"""Dense sampling of a flagged window: the frames above threshold inside it (#133, #141).

Shared by the scanner (as a cluster closes) and the backfill job (for segments stored before
dense sampling existed), so both pick thumbnails and bounds the same way.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

from .logger import get_logger

logger = get_logger(__name__)

# A window is sampled at refine_step_ms but never into more frames than this, so a long
# segment costs a bounded amount: 120 frames is about 2 s of NudeNet at 320 px.
REFINE_MAX_FRAMES = 120

# (at_ms, is_nude, score, labels, jpeg): a frame at an absolute film time that scored a hit.
Hit = tuple[int, bool, float, list[str], bytes]
# jpeg, at_ms -> (is_nude, siglip_hit, score, labels)
Judge = Callable[[bytes, int], Awaitable[tuple[bool, bool, float, list[str]]]]
Extract = Callable[..., AsyncIterator[tuple[int, bytes | None]]]


async def dense_hits(
    *,
    part_file: str,
    part_offset_ms: int,
    part_end_ms: int,
    start_ms: int,
    last_ms: int,
    coarse_step_ms: int,
    gap_ms: int,
    refine_step_ms: int,
    frame_size: int,
    hardware: bool,
    judge: Judge,
    extract: Extract,
    label: str = "",
) -> list[Hit]:
    """Re-sample [start_ms - one coarse step, last_ms + one coarse step] and return the hits.

    start_ms and last_ms are the first and last flagged times, absolute in the film;
    part_offset_ms is where *part_file* begins in it and part_end_ms where it ends. A hit at
    the window's edge means the scene runs on, so the search continues, up to one merge gap
    each way, in chunks of two coarse steps. Best effort: a failing extraction is logged and
    leaves whatever was found.
    """

    async def scan(lo_ms: int, hi_ms: int, dstep: int) -> list[Hit]:
        found: list[Hit] = []
        try:
            async for rel_ms, jpeg in extract(
                part_file, lo_ms - part_offset_ms, hi_ms - lo_ms, dstep, frame_size, hardware
            ):
                if not jpeg:
                    continue
                at_ms = part_offset_ms + rel_ms
                is_nude, siglip_hit, score, labels = await judge(jpeg, at_ms)
                if is_nude or siglip_hit:
                    found.append((at_ms, is_nude, score, labels, jpeg))
        except Exception as exc:
            logger.warning("Re-sampling %d-%dms of '%s' failed, keeping what was found: %s", lo_ms, hi_ms, label, exc)
        return found

    lo = max(part_offset_ms, start_ms - coarse_step_ms)
    hi = min(part_end_ms, last_ms + coarse_step_ms)
    dstep = max(refine_step_ms, -(-(hi - lo) // REFINE_MAX_FRAMES))
    hits = await scan(lo, hi, dstep)
    chunk = max(2 * coarse_step_ms, 4 * dstep)
    grown = 0
    while hits and grown < gap_ms and hi < part_end_ms and max(h[0] for h in hits) >= hi - 2 * dstep:
        new_hi = min(part_end_ms, hi + chunk)
        more = await scan(hi, new_hi, dstep)
        hits += more
        grown += new_hi - hi
        hi = new_hi
        if not more:
            break
    grown = 0
    while hits and grown < gap_ms and lo > part_offset_ms and min(h[0] for h in hits) <= lo + 2 * dstep:
        new_lo = max(part_offset_ms, lo - chunk)
        more = await scan(new_lo, lo, dstep)
        hits += more
        grown += lo - new_lo
        lo = new_lo
        if not more:
            break
    return hits
