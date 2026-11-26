"""Push planning: derives desired observations from local state and diffs against hayaa_sent (#234)."""

from __future__ import annotations

from typing import Any

from .. import database as db
from ..importers._common import severity_for_labels
from .models import (
    FAMILY_CATEGORY,
    FAMILY_OF_SOURCE,
    KNOWN_LABELS,
    OBS_PER_PUSH,
    HayaaClaim,
    LedgerEntry,
    Observation,
    PushPlan,
    SyncTitle,
    obs_id,
    overlaps,
)


def _labels(value: str) -> list[str]:
    return [lbl for lbl in dict.fromkeys((value or "").split(",")) if lbl in KNOWN_LABELS][:8]


def _secs(ms: int) -> float:
    return round(ms / 1000.0, 3)


class PushPlanner:
    """Derives outgoing observations for a title and diffs them against its sent-ledger."""

    def match_claim(self, seg: dict[str, Any], claims: list[HayaaClaim]) -> HayaaClaim | None:
        """The claim a segment belongs to: its stored link if that claim still exists, else the best overlap."""
        by_id = {c.claim_id: c for c in claims}
        claim_id = seg.get("hayaa_claim_id") or ""
        if claim_id and claim_id in by_id:
            return by_id[claim_id]
        best: HayaaClaim | None = None
        best_overlap = -1
        for c in claims:
            if overlaps(seg["start_ms"], seg["end_ms"], c.start_ms, c.end_ms):
                ov = min(seg["end_ms"], c.end_ms) - max(seg["start_ms"], c.start_ms)
                if ov > best_overlap:
                    best, best_overlap = c, ov
        return best

    def desired_observations(
        self,
        title_key: str,
        title: SyncTitle,
        segments: list[dict[str, Any]],
        claims: list[HayaaClaim],
        overrides: dict[tuple[str, str], str],
        nudenet_detector: str,
    ) -> dict[str, Observation]:
        """Return {base_id: Observation} for one title (without generation ids)."""
        runtime = (title.duration_ms or 0) / 1000.0 or None
        scan_id = obs_id("scan", title.plex_guid, title.finished_at)
        out: dict[str, Observation] = {}

        def add(base: str, kind: str, body: dict[str, Any]) -> None:
            if runtime and body.get("end") is not None and body["end"] > runtime:
                return  # Hayaa refuses ranges past the runtime; the duration Plex reports can be short
            payload = dict(body)
            if runtime:
                payload["runtime"] = round(runtime, 3)
            out[base] = Observation(base_id=base, title_key=title_key, kind=kind, body=payload)

        machine_found = False
        for seg in segments:
            fam = FAMILY_OF_SOURCE.get(seg.get("source") or "")
            fp = seg.get("false_positive") or 0
            if fam in ("nudenet", "siglip"):
                # The scan found something, even if the user or the verifier later dismissed it, so
                # the title is not "clean"; a dismissal is expressed by the reject vote instead.
                machine_found = True
            if fam and fp in (0, db.FP_COMMUNITY):
                body: dict[str, Any] = {
                    "start": _secs(seg["start_ms"]),
                    "end": _secs(seg["end_ms"]),
                    "detector": fam,
                    "scan_id": scan_id,
                    "category": FAMILY_CATEGORY[fam],
                }
                if fam == "subtitles":
                    # The matched words are never shared; Hayaa takes a severity instead.
                    body["detector_version"] = "subtitles"
                    body["severity"] = seg.get("severity") or "high"
                else:
                    labels = _labels(seg.get("detected_labels") or seg.get("labels") or "")
                    if not labels:
                        continue
                    body["labels"] = labels
                    body["detector_version"] = seg.get("detector") or nudenet_detector
                    conf = float(seg.get("confidence") or 0)
                    if fam == "siglip" and conf >= 1.0:
                        conf -= 1.0  # stored SigLIP scores are offset by +1 to rank below NudeNet
                    body["confidence"] = round(min(1.0, max(0.0, conf)), 4)
                add(obs_id("detect", title_key, fam, seg["start_ms"], seg["end_ms"]), "detect", body)

            claim = self.match_claim(seg, claims) if claims else None
            if claim is None:
                continue
            cid = claim.claim_id
            if fp == db.FP_MARKED and (fam or seg.get("source") == "hayaa"):
                add(obs_id("reject", title_key, cid), "reject", {"claim_id": cid})
            corrected = _labels(seg.get("corrected_labels") or "")
            if corrected and (fam in ("nudenet", "siglip") or seg.get("source") == "hayaa"):
                add(
                    obs_id("label", title_key, cid, *sorted(corrected)),
                    "label",
                    {"claim_id": cid, "labels": corrected, "severity": severity_for_labels(",".join(corrected))},
                )

        if title.status == "done" and title.scan_version > 0 and not machine_found:
            add(
                obs_id("clean", title_key, title.scan_version, title.finished_at),
                "clean",
                {"detector": "nudenet", "detector_version": nudenet_detector, "scan_id": scan_id},
            )

        for c in claims:
            cid = c.claim_id
            if overrides.get((title_key, cid)) == "keep":
                add(obs_id("confirm", title_key, cid), "confirm", {"claim_id": cid})
            if c.verify_state in ("hit", "miss") and c.verify_family:
                verify_body: dict[str, Any] = {
                    "claim_id": cid,
                    "detector": c.verify_family,
                    "detector_version": nudenet_detector,
                }
                if c.verify_family == "siglip":
                    verify_body["detector_version"] = "siglip-base-224"
                if c.verify_conf is not None:
                    verify_body["confidence"] = round(min(1.0, max(0.0, float(c.verify_conf))), 4)
                add(obs_id("verify", title_key, cid, c.verify_state), f"verify_{c.verify_state}", verify_body)
        return out

    def plan_push(
        self,
        desired: dict[str, Observation],
        ledger: list[LedgerEntry],
        title_key: str,
    ) -> PushPlan:
        """Diff one title's desired observations against what was sent."""
        live = {r.base_id: r for r in ledger if r.live and r.kind != "retract"}
        generations: dict[str, int] = {}
        for r in ledger:
            generations[r.base_id] = generations.get(r.base_id, 0) + 1
        push: list[dict[str, Any]] = []
        rows: list[tuple[str, str, str, str]] = []
        for base, obs in desired.items():
            if base in live:
                continue
            gen = generations.get(base, 0)
            oid = base if gen == 0 else f"{base}-{gen}"
            push.append(obs.to_wire(oid))
            rows.append((oid, base, title_key, obs.kind))
        retract = sorted(r.obs_id for base, r in live.items() if base not in desired)
        for i in range(0, len(retract), OBS_PER_PUSH):
            chunk = retract[i:i + OBS_PER_PUSH]
            rid = obs_id("retract", *chunk)
            push.append({"id": rid, "title": title_key, "kind": "retract", "retracts": chunk})
            rows.append((rid, rid, title_key, "retract"))
        return PushPlan(observations=push, ledger_rows=rows, retracted_ids=retract)
