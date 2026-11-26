"""Uploading media descriptors, observations and possession proofs to Hayaa (#234)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from .. import database as db
from .. import hayaa
from ..logger import get_logger
from .models import (
    BODY_BUDGET,
    MEDIA_PER_PUSH,
    OBS_PER_PUSH,
    PULL_BATCH,
    HayaaClaim,
    HayaaTitleRecord,
    LedgerEntry,
    SyncResult,
    SyncTitle,
    chunk_by_size,
    item_json_size,
)
from .planner import PushPlanner
from .quota import QuotaTracker

logger = get_logger(__name__)


async def _nudenet_detector() -> str:
    model = await db.get_setting("nudenet_model", "320n") or "320n"
    custom = bool(await db.get_setting("nudenet_model_path", ""))
    return f"nudenet-{model}" + ("-custom" if custom else "")


class HayaaPusher:
    """Sends media descriptors, observation diffs and possession challenge answers."""

    def __init__(self, planner: PushPlanner, quota: QuotaTracker) -> None:
        self._planner = planner
        self._quota = quota

    async def describe(
        self,
        by_key: dict[str, SyncTitle],
        known: dict[str, HayaaTitleRecord],
    ) -> int:
        """Send media descriptors (release key, runtime) for files Hayaa has not been told about."""
        todo = [k for k in by_key if not (known.get(k) and known[k].described)]
        sent = 0
        # Describing files is not a push on Hayaa's side (it has its own daily media quota), so it
        # spends none of the local push allowance.
        for i in range(0, len(todo), MEDIA_PER_PUSH):
            chunk = todo[i:i + MEDIA_PER_PUSH]
            media: list[dict[str, Any]] = []
            for k in chunk:
                t = by_key[k]
                try:
                    size = t.content_size if t.removed else (await asyncio.to_thread(os.stat, t.file_path)).st_size
                except OSError:
                    continue
                d: dict[str, Any] = {
                    "title": k,
                    "release_key": hayaa.release_key(Path(t.file_path).name, size),
                }
                if t.duration_ms:
                    d["runtime"] = round(t.duration_ms / 1000.0, 3)
                media.append(d)
            for part in chunk_by_size(media):
                await hayaa.signed_post("/api/v1/media", {"media": part})
                await db.mark_hayaa_described([m["title"] for m in part])
                sent += len(part)
        return sent

    async def push(
        self,
        by_key: dict[str, SyncTitle],
        manual: bool,
        result: SyncResult,
    ) -> bool:
        """Send what changed for these versions/titles; True when Hayaa accepted all of it (202 queued)."""
        guids = sorted({t.plex_guid for t in by_key.values()})
        live_vids = [t.version_id for t in by_key.values() if not t.removed and t.version_id]
        rem_vids = [t.version_id for t in by_key.values() if t.removed and t.version_id]
        fallback_live = [t.plex_guid for t in by_key.values() if not t.removed and not t.version_id]
        fallback_rem = [t.plex_guid for t in by_key.values() if t.removed and not t.version_id]

        raw_segs = (
            (await db.get_segments_for_hayaa(live_vids) if live_vids else [])
            + (await db.get_segments_for_hayaa(fallback_live) if fallback_live else [])
            + (await db.get_removed_segments_for_hayaa(rem_vids) if rem_vids else [])
            + (await db.get_removed_segments_for_hayaa(fallback_rem) if fallback_rem else [])
        )
        segs_by_vid: dict[int, list[dict[str, Any]]] = {}
        segs_by_guid: dict[str, list[dict[str, Any]]] = {}
        for s in raw_segs:
            vid = int(s.get("version_id") or 0)
            if vid:
                segs_by_vid.setdefault(vid, []).append(s)
            segs_by_guid.setdefault(s["plex_guid"], []).append(s)

        claims_by_key: dict[str, list[HayaaClaim]] = {}
        for row in await db.get_hayaa_claims(guids):
            c = HayaaClaim.from_row(row)
            claims_by_key.setdefault(c.title_key, []).append(c)
        keys = list(by_key)
        overrides = await db.get_hayaa_overrides(keys)
        ledger: dict[str, list[LedgerEntry]] = {}
        for row in await db.get_hayaa_sent(keys):
            entry = LedgerEntry.from_row(row)
            ledger.setdefault(entry.title_key, []).append(entry)
        detector = await _nudenet_detector()

        batch: list[dict[str, Any]] = []
        rows: list[tuple[str, str, str, str]] = []
        retracted: list[str] = []

        async def flush() -> bool:
            nonlocal batch, rows, retracted
            if not batch:
                return True
            if self._quota.allowance(await self._quota.current(), "pushes", manual) <= 0:
                result.skipped_quota = True
                return False
            await self._quota.spend("pushes")
            await hayaa.signed_post("/api/v1/push", {"observations": batch})  # 202: queued on Hayaa
            await db.record_hayaa_sent(rows, retracted)
            result.pushed += sum(1 for o in batch if o["kind"] != "retract")
            result.retracted += len(retracted)
            batch, rows, retracted = [], [], []
            return True

        for key, t in by_key.items():
            title_segs = segs_by_vid.get(t.version_id, []) if t.version_id else segs_by_guid.get(t.plex_guid, [])
            want = self._planner.desired_observations(
                key, t, title_segs, claims_by_key.get(key, []), overrides, detector
            )
            plan = self._planner.plan_push(want, ledger.get(key, []), key)
            if not plan.observations:
                continue
            if (
                batch
                and (
                    len(batch) + len(plan.observations) > OBS_PER_PUSH
                    or sum(map(item_json_size, batch)) + sum(map(item_json_size, plan.observations)) > BODY_BUDGET
                )
                and not await flush()
            ):
                return False
            batch += plan.observations
            rows += plan.ledger_rows
            retracted += plan.retracted_ids
        return await flush()

    async def prove(
        self,
        by_key: dict[str, SyncTitle],
        challenges: dict[str, list[int]],
        result: SyncResult,
    ) -> None:
        """Answer Hayaa's possession challenges from the files themselves (ABUSE_RESISTANCE §2).

        Each answer reads one 64 KB chunk; a proofs-only push costs no daily push quota on Hayaa.
        """
        proofs: dict[str, dict[str, str]] = {}
        for key, positions in challenges.items():
            t = by_key.get(key)
            if t is None:
                continue
            try:
                proofs[key] = await asyncio.to_thread(
                    hayaa.proofs_for_file, Path(t.file_path), list(positions)
                )
            except (OSError, ValueError) as exc:
                logger.debug("Cannot prove %s: %s", t.title, exc)
        keys = list(proofs)
        for i in range(0, len(keys), PULL_BATCH):
            chunk = {k: proofs[k] for k in keys[i:i + PULL_BATCH]}
            await hayaa.signed_post("/api/v1/push", {"observations": [], "proofs": chunk})
            result.proved += len(chunk)
        challenges.clear()
