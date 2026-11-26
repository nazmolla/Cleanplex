"""Apply matrix: reconciles staged Hayaa claims with local segments and user overrides (#234)."""

from __future__ import annotations

from typing import Any

from .. import database as db
from .. import events, plex_summary, segment_files
from ..logger import get_logger
from .models import (
    DISAGREE_MIN_SAMPLES,
    DISAGREE_SWITCH,
    FAMILY_OF_SOURCE,
    PRESETS,
    ApplyCounts,
    HayaaClaim,
    overlaps,
)

logger = get_logger(__name__)


def _claim_labels(claim: HayaaClaim) -> str:
    """A claim's labels: Hayaa sends none any more (ABUSE_RESISTANCE §7), so the labels this
    server's own re-check found stand in. Without them the global thresholds apply."""
    return claim.labels or claim.verify_labels or ""


class ClaimApplier:
    """Applies staged claims to local segments using the user's preset and overrides."""

    async def get_preset(self) -> str:
        preset = await db.get_setting("hayaa_preset", "balanced")
        return preset if preset in PRESETS else "balanced"

    @staticmethod
    def threshold_ok(claim: HayaaClaim, config: Any) -> bool:
        """A machine-only claim acts only if a detector's best confidence passes this instance's threshold."""
        if claim.basis != "machine":
            return True
        labels = [lbl for lbl in _claim_labels(claim).split(",") if lbl]
        for fam, conf in claim.machine.items():
            if fam == "subtitles":
                return True
            if fam == "nudenet" and any(conf >= config.threshold_for(lbl) for lbl in labels or [""]):
                return True
            if fam == "siglip" and any(conf >= config.siglip_threshold_for(lbl) for lbl in labels or [""]):
                return True
        return not claim.machine

    @staticmethod
    def decide(claim: HayaaClaim, override: str | None, preset: str, thresholds_pass: bool) -> str:
        """Apply matrix for a claim no local segment covers: 'apply', 'verify', 'hold' or 'skip'."""
        if override == "delete":
            return "skip"
        if override == "keep":
            return "apply"
        status, v = claim.status, claim.verify_state
        if status == "rejected":
            return "skip"
        human = claim.basis in ("human", "both")
        if preset == "verify_first":
            return "apply" if v == "hit" else ("verify" if v in ("", "pending") else "hold")
        if not human and not thresholds_pass:
            return "skip"
        if status == "accepted":
            same_detector_only = set(claim.machine) == {claim.verify_family}
            if v == "miss" and not human and same_detector_only:
                return "hold"  # the local detector should have reproduced it and did not
            return "apply"
        if status == "candidate":
            if v == "hit" or preset == "cautious":
                return "apply"
            return "verify" if v in ("", "pending") else "hold"
        # disputed: applies unless the local false-positive model objects to the verified frame
        if v == "hit" and claim.verify_fp_drop:
            return "hold"
        return "apply"

    @staticmethod
    def needs_verify(claim: HayaaClaim) -> bool:
        """Frames can be re-checked for nudity claims; subtitle (language) claims cannot."""
        return claim.status != "rejected" and claim.category != "language"

    @staticmethod
    async def _title_of(guid: str) -> str:
        job = await db.get_scan_job_by_guid(guid)
        return (job or {}).get("title") or ""

    async def apply_titles(self, plex_guids: list[str], config: Any = None) -> ApplyCounts:
        """Bring segments in line with the staged claims of these titles; returns counts by outcome."""
        from ..config import Config  # import here: config imports database at module load

        config = config or await Config.load()
        preset = await self.get_preset()
        claims_all = [HayaaClaim.from_row(r) for r in await db.get_hayaa_claims(plex_guids)]
        segs_all = await db.get_segments_for_hayaa(plex_guids)
        versions_by_guid = await db.get_title_versions_for_guids(plex_guids)

        def _norm_vid(guid: str, vid: int) -> int:
            if vid > 0:
                return vid
            vlist = versions_by_guid.get(guid, [])
            return int(vlist[0]["id"]) if len(vlist) == 1 else 0

        by_scope: dict[tuple[str, int], list[HayaaClaim]] = {}
        for c in claims_all:
            by_scope.setdefault((c.plex_guid, _norm_vid(c.plex_guid, c.version_id)), []).append(c)
        segs_by_scope: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for s in segs_all:
            segs_by_scope.setdefault((s["plex_guid"], _norm_vid(s["plex_guid"], int(s.get("version_id") or 0))), []).append(s)
        keys = {c.title_key for c in claims_all}
        overrides = await db.get_hayaa_overrides(sorted(keys)) if keys else {}

        scopes: list[tuple[str, int]] = sorted(
            set(by_scope)
            | {(s["plex_guid"], _norm_vid(s["plex_guid"], int(s.get("version_id") or 0)))
               for s in segs_all if s.get("source") == "hayaa"}
            | {(g, _norm_vid(g, 0)) for g in plex_guids if not any(cg == g for cg, _ in by_scope)}
        )

        counts = ApplyCounts()
        changed: set[str] = set()
        for guid, vid in scopes:
            claims = by_scope.get((guid, vid), [])
            segs = segs_by_scope.get((guid, vid), [])
            own = [s for s in segs if s.get("source") != "hayaa"]
            pulled = {s["hayaa_claim_id"]: s for s in segs if s.get("source") == "hayaa" and s.get("hayaa_claim_id")}
            links: list[tuple[str, int]] = []
            title = next((s.get("title") for s in segs if s.get("title")), "")
            claim_ids = {c.claim_id for c in claims}

            for c in claims:
                tk, cid = c.title_key, c.claim_id
                ov = overrides.get((tk, cid))
                local = [s for s in own if overlaps(s["start_ms"], s["end_ms"], c.start_ms, c.end_ms)]
                if local:
                    decision = "local"
                    for s in local:
                        if s.get("hayaa_claim_id") != cid:
                            links.append((cid, s["id"]))
                            counts.linked += 1
                    if cid in pulled and (pulled[cid].get("false_positive") or 0) != db.FP_MARKED:
                        await segment_files.delete_segment(pulled[cid]["id"])
                        counts.removed += 1
                        changed.add(guid)
                    hide = (
                        c.status == "rejected"
                        and preset != "cautious"
                        and ov != "restore"
                        and (c.severity in ("minimal", "low", "medium") or bool(c.verify_fp_drop))
                    )
                    ids_on = [
                        s["id"]
                        for s in local
                        if (s.get("false_positive") or 0) == 0 and s.get("source") in FAMILY_OF_SOURCE
                    ]
                    ids_hidden = [s["id"] for s in local if s.get("false_positive") == db.FP_COMMUNITY]
                    if hide and ids_on:
                        await db.set_segments_false_positive(ids_on, db.FP_COMMUNITY)
                        counts.hidden += len(ids_on)
                        changed.add(guid)
                    elif not hide and ids_hidden:
                        await db.set_segments_false_positive(ids_hidden, 0)
                        counts.restored += len(ids_hidden)
                        changed.add(guid)
                    if c.status == "rejected" and c.severity == "high" and not hide and ids_on:
                        decision = "ask"  # high severity: kept until the local model or the user agrees
                else:
                    decision = self.decide(c, ov, preset, self.threshold_ok(c, config))
                    existing = pulled.get(cid)
                    if (
                        decision == "apply"
                        and existing is not None
                        and _claim_labels(c)
                        and await db.fill_hayaa_segment_labels(existing["id"], _claim_labels(c))
                    ):
                        changed.add(guid)
                    if decision == "apply" and existing is None:
                        labels = _claim_labels(c)
                        language = c.category == "language"
                        seg_id = await db.insert_segment(
                            plex_guid=guid,
                            title=title or await self._title_of(guid),
                            start_ms=c.start_ms,
                            end_ms=c.end_ms,
                            confidence=max(c.machine.values(), default=0.0),
                            thumbnail_path=c.verify_thumb or None,
                            labels=labels,
                            category=c.category or "nudity",
                            severity=c.severity or "high",
                            action="mute" if language else "skip",
                            channel="audio" if language else "both",
                            source="hayaa",
                            detector="hayaa",
                            version_id=vid or None,
                        )
                        links.append((cid, seg_id))
                        counts.applied += 1
                        changed.add(guid)
                    elif (
                        decision != "apply"
                        and existing is not None
                        and (existing.get("false_positive") or 0) != db.FP_MARKED
                    ):
                        await segment_files.delete_segment(existing["id"])
                        counts.removed += 1
                        changed.add(guid)
                    if decision in ("hold", "verify"):
                        counts.held += 1
                if c.decision != decision:
                    await db.update_hayaa_claim(c.title_key, cid, decision=decision)
                if self.needs_verify(c) and not c.verify_state and decision != "local":
                    await db.update_hayaa_claim(c.title_key, cid, verify_state="pending")
                elif not self.needs_verify(c) and not c.verify_state:
                    await db.update_hayaa_claim(c.title_key, cid, verify_state="na")

            # A pulled segment whose claim is gone from the view (merged, purged) goes too.
            for cid, s in pulled.items():
                if cid not in claim_ids and (s.get("false_positive") or 0) != db.FP_MARKED:
                    await segment_files.delete_segment(s["id"])
                    counts.removed += 1
                    changed.add(guid)
            await db.set_segment_hayaa_claims(links)

        for guid in changed:
            events.publish("segments_changed", guid=guid)
            plex_summary.refresh_soon(guid)
        return counts

    async def check_disagreement(self) -> None:
        """Switch to verify_first when local re-checks keep contradicting machine-only accepted claims."""
        claims = [
            HayaaClaim.from_row(r)
            for r in await db.get_hayaa_claims()
            if r["status"] == "accepted"
            and r["basis"] == "machine"
            and r.get("verify_state") in ("hit", "miss")
        ]
        if len(claims) < DISAGREE_MIN_SAMPLES:
            return
        misses = sum(1 for c in claims if c.verify_state == "miss")
        if misses / len(claims) > DISAGREE_SWITCH and await self.get_preset() != "verify_first":
            await db.set_setting("hayaa_preset", "verify_first")
            await db.set_setting("hayaa_preset_auto_switched", "1")
            logger.warning(
                "Hayaa: local re-checks disagree with %d of %d shared claims; switched to Verify first",
                misses,
                len(claims),
            )
            events.publish("hayaa_sync", phase="preset_switched")

    async def on_segment_deleted(self, seg: dict[str, Any]) -> None:
        """A user deleted a pulled segment: remember it so later pulls never bring it back."""
        if seg.get("source") == "hayaa" and seg.get("hayaa_claim_id"):
            vid = int(seg.get("version_id") or 0) or None
            key = await db.get_hayaa_title_key(seg["plex_guid"], version_id=vid)
            if key:
                await db.set_hayaa_override(key, seg["hayaa_claim_id"], "delete")

    async def on_false_positive_cleared(self, seg: dict[str, Any]) -> None:
        """A user restored a segment Hayaa's community had hidden: keep it shown from now on."""
        if seg.get("false_positive") == db.FP_COMMUNITY and seg.get("hayaa_claim_id"):
            vid = int(seg.get("version_id") or 0) or None
            key = await db.get_hayaa_title_key(seg["plex_guid"], version_id=vid)
            if key:
                await db.set_hayaa_override(key, seg["hayaa_claim_id"], "restore")

    async def review(self, title_key: str, claim_id: str, keep: bool) -> dict[str, int]:
        """Keep (confirm) or dismiss a held claim from the review list, then apply its title."""
        await db.set_hayaa_override(title_key, claim_id, "keep" if keep else "delete")
        claims = [
            c
            for c in await db.get_hayaa_claims()
            if c["title_key"] == title_key and c["claim_id"] == claim_id
        ]
        if not claims:
            return {}
        return (await self.apply_titles([claims[0]["plex_guid"]])).to_dict()
