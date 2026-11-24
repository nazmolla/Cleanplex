"""Local re-check of pulled Hayaa claims (#222, CONFLICTS §7.1).

For each staged claim no local segment covers, sample the claimed window plus a margin with the
local detectors and thresholds (the same dense sampling as segment refinement). A hit gives the
pulled segment a local thumbnail and lets candidate claims apply; a miss is reported. For a
disputed claim the local false-positive model judges the best frame. Cost follows marked time,
not runtime. Runs inside the scan window, like the other detection jobs.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable

from . import database as db
from . import hayaa_sync, scanner, verifier
from .config import Config
from .frame_extractor import extract_frames_window, get_duration_ms
from .logger import get_logger
from .segment_refine import dense_hits

logger = get_logger(__name__)

MARGIN_MS = 5000  # CONFLICTS §7.1: the claimed window plus 5 s each side
STEP_MS = 500
# A window this long is not re-checked frame by frame; Hayaa's size guards keep such claims
# candidate, so they stay held rather than costing a scan of half a film.
MAX_WINDOW_MS = 10 * 60 * 1000
BATCH = 20
CHECK_INTERVAL_S = 60


async def verify_claim(claim: dict, config: Config, detectors: tuple[str, str, bool, bool]) -> str:
    """Re-check one claim; returns the new verify_state ('hit', 'miss', 'na') or 'offline'."""
    path = claim.get("file_path") or ""
    if not path or not await asyncio.to_thread(os.path.isfile, path):
        return "offline"
    if claim["end_ms"] - claim["start_ms"] > MAX_WINDOW_MS:
        await db.update_hayaa_claim(claim["title_key"], claim["claim_id"], verify_state="na")
        return "na"
    duration = claim.get("duration_ms") or await get_duration_ms(path) or 0
    if not duration:
        await db.update_hayaa_claim(claim["title_key"], claim["claim_id"], verify_state="na")
        return "na"
    nudenet_model, nudenet_model_path, _custom, siglip_on = detectors
    judge = scanner.make_judge(config, nudenet_model, nudenet_model_path, siglip_on)
    hits = await dense_hits(
        part_file=path, part_offset_ms=0, part_end_ms=duration,
        start_ms=max(0, claim["start_ms"] - MARGIN_MS), last_ms=min(duration, claim["end_ms"] + MARGIN_MS),
        # gap 0: verify the claimed window only, never grow it the way refinement does
        coarse_step_ms=STEP_MS, gap_ms=0, refine_step_ms=STEP_MS,
        frame_size=scanner.frame_size_for(nudenet_model, siglip_on),
        hardware=bool(config.hw_decode), judge=judge, extract=extract_frames_window,
        label=claim.get("title") or claim["plex_guid"],
    )
    if not hits:
        await db.update_hayaa_claim(claim["title_key"], claim["claim_id"], verify_state="miss",
                                    verify_family="nudenet", verify_conf=None)
        return "miss"
    nudenet_hits = [h for h in hits if h[1]]
    family = "nudenet" if nudenet_hits else "siglip"
    best = max(nudenet_hits or hits, key=lambda h: h[2])
    # judge() ranks SigLIP scores 1.0 below NudeNet's; undo that for the reported confidence.
    conf = best[2] if family == "nudenet" else best[2] + 1.0
    scanner.THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)
    thumb = scanner.THUMBNAILS_DIR / f"hayaa_{re.sub(r'[^0-9a-f]', '', claim['claim_id'])[:32]}_{best[0]}.jpg"
    await asyncio.to_thread(thumb.write_bytes, best[4])
    fp_drop = 0
    if claim["status"] == "disputed" and config.verifier_loss_pct:
        group = verifier.group_key(family, list(best[3]))
        drop, _score = await scanner._infer(verifier.judge_jpeg, best[4], group, config.verifier_loss_pct)
        fp_drop = 1 if drop else 0
    await db.update_hayaa_claim(claim["title_key"], claim["claim_id"], verify_state="hit", verify_family=family,
                                verify_conf=round(conf, 4), verify_thumb=str(thumb), verify_fp_drop=fp_drop,
                                verify_labels=",".join(best[3]))
    return "hit"


async def verify_pending(get_config_fn: Callable[[], Awaitable[Config]],
                         active: Callable[[], Awaitable[bool]]) -> int:
    """Re-check waiting claims in batches while *active* holds; returns how many were looked at."""
    config = await get_config_fn()
    detectors = await scanner.prepare_detectors(config, "Hayaa verify")
    done = 0
    while await active():
        claims = await db.get_hayaa_claims_to_verify(BATCH)
        if not claims:
            break
        touched: set[str] = set()
        progressed = False
        for claim in claims:
            if not await active():
                break
            state = await verify_claim(claim, config, detectors)
            if state == "offline":
                # Moves it to the back of the queue (updated_at) so reachable files go first.
                await db.update_hayaa_claim(claim["title_key"], claim["claim_id"], verify_state="pending")
                continue
            progressed = True
            done += 1
            touched.add(claim["plex_guid"])
        if touched:
            await hayaa_sync.apply_titles(sorted(touched), config)
        if not progressed:
            break  # every waiting file is offline; try again next round
    return done


async def hayaa_verify_loop(get_config_fn: Callable[[], Awaitable[Config]]) -> None:
    """Background job: re-check pulled claims whenever the scan window is open and some wait."""

    async def active() -> bool:
        return (await get_config_fn()).is_scan_window()

    while True:
        try:
            if await active() and await db.count_hayaa_claims(verify_state="pending"):
                n = await verify_pending(get_config_fn, active)
                if n:
                    logger.info("Hayaa verify: re-checked %d claim(s)", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Hayaa verify run failed")
        await asyncio.sleep(CHECK_INTERVAL_S)
