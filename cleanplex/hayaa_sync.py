"""Hayaa sync (#222): turns local state into observations, pushes them, pulls consensus, applies it.

Outgoing data is derived, not queued: for every synced title the current segments, verdicts and
verify results are turned into the set of observations this instance asserts now
(``desired_observations``), and the sent-ledger (``hayaa_sent``) says what Hayaa already has.
New assertions are pushed and ones that disappeared are retracted. Scans, deletes, rescans and
purges therefore never need their own sync hooks.

Incoming claims are staged in ``hayaa_claims`` and only reach ``segments`` through the apply
matrix (``decide``), with the user's own verdicts (``hayaa_overrides``) consulted first. Rules:
Hayaa's docs/CONFLICTS.md §3 and §7.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import database as db
from . import events, hayaa, plex_summary, segment_files
from .importers._common import CATEGORIES, DETECTOR_LABELS, severity_for_labels
from .logger import get_logger

logger = get_logger(__name__)

# Segment sources that are this instance's own detections. Imported skip files never leave the
# instance (§5.4), and pulled segments ('hayaa') are not ours to assert.
FAMILY_OF_SOURCE = {"scanner": "nudenet", "siglip": "siglip", "subtitles": "subtitles"}
FAMILY_CATEGORY = {"nudenet": "nudity", "siglip": "nudity", "subtitles": "language"}
# Same overlap tolerance Hayaa clusters with, so a local segment and a claim it supports match.
TOLERANCE_MS = 2000

# Hayaa's per-user daily limits (hayaa.rules.config.LimitsConfig) and request caps.
PUSHES_PER_DAY = 20
PULLS_PER_DAY = 60
# Automatic sync keeps a fifth of each daily quota free for a manual sync.
AUTO_BUDGET = 0.8
OBS_PER_PUSH = 2000
MEDIA_PER_PUSH = 2000
PULL_BATCH = 500
DEFAULT_PAUSE_S = 3600  # when a 429/503 carries no Retry-After

PRESETS = ("balanced", "cautious", "verify_first")
# Verify results on machine-only accepted claims: above this share of misses the service is
# not trusted for this instance and the preset drops to verify_first (CONFLICTS §7.2).
DISAGREE_SWITCH = 0.10
DISAGREE_MIN_SAMPLES = 20

_KNOWN_LABELS = set(DETECTOR_LABELS)


def obs_id(*parts: object) -> str:
    """Stable observation id (32 hex, inside Hayaa's 8-64 char id rule)."""
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:32]


def overlaps(a_start: int, a_end: int, b_start: int, b_end: int, tol: int = TOLERANCE_MS) -> bool:
    return a_start < b_end + tol and b_start < a_end + tol


def _labels(value: str) -> list[str]:
    return [lbl for lbl in dict.fromkeys((value or "").split(",")) if lbl in _KNOWN_LABELS][:8]


def _secs(ms: int) -> float:
    return round(ms / 1000.0, 3)


def match_claim(seg: dict, claims: list[dict]) -> dict | None:
    """The claim a segment belongs to: its stored link if that claim still exists, else the best overlap."""
    by_id = {c["claim_id"]: c for c in claims}
    if seg.get("hayaa_claim_id") and seg["hayaa_claim_id"] in by_id:
        return by_id[seg["hayaa_claim_id"]]
    best, best_overlap = None, -1
    for c in claims:
        if overlaps(seg["start_ms"], seg["end_ms"], c["start_ms"], c["end_ms"]):
            ov = min(seg["end_ms"], c["end_ms"]) - max(seg["start_ms"], c["start_ms"])
            if ov > best_overlap:
                best, best_overlap = c, ov
    return best


# ── Outgoing: what this instance asserts now ───────────────────────────────────

def desired_observations(title_key: str, job: dict, segments: list[dict], claims: list[dict],
                         overrides: dict[tuple[str, str], str], nudenet_detector: str) -> dict[str, dict]:
    """Return {base_id: observation} for one title (without the final 'id', see plan_push).

    - detect: each active own detection (user false positives and verifier rejections excluded;
      a community-hidden one is still a fact this instance saw).
    - clean: a finished scan that found nothing.
    - reject: a user false positive on a segment that belongs to a claim.
    - label: a user relabel on a segment that belongs to a claim.
    - confirm: the user kept a pulled claim.
    - verify_hit / verify_miss: the local re-check of a claim.
    """
    runtime = (job.get("duration_ms") or 0) / 1000.0 or None
    scan_id = obs_id("scan", job["plex_guid"], job.get("finished_at") or "")
    out: dict[str, dict] = {}

    def add(base: str, obs: dict) -> None:
        obs = {"title": title_key, **obs}
        if runtime and obs.get("end") is not None and obs["end"] > runtime:
            return  # Hayaa refuses ranges past the runtime; the duration Plex reports can be short
        if runtime:
            obs["runtime"] = round(runtime, 3)
        out[base] = obs

    machine_found = False
    for seg in segments:
        fam = FAMILY_OF_SOURCE.get(seg.get("source") or "")
        fp = seg.get("false_positive") or 0
        if fam in ("nudenet", "siglip"):
            # The scan found something, even if the user or the verifier later dismissed it, so
            # the title is not "clean"; a dismissal is expressed by the reject vote instead.
            machine_found = True
        if fam and fp in (0, db.FP_COMMUNITY):
            obs: dict[str, Any] = {
                "kind": "detect", "start": _secs(seg["start_ms"]), "end": _secs(seg["end_ms"]),
                "detector": fam, "scan_id": scan_id, "category": FAMILY_CATEGORY[fam],
            }
            if fam == "subtitles":
                # The matched words are never shared; Hayaa takes a severity instead.
                obs["detector_version"] = "subtitles"
                obs["severity"] = seg.get("severity") or "high"
            else:
                labels = _labels(seg.get("detected_labels") or seg.get("labels") or "")
                if not labels:
                    continue
                obs["labels"] = labels
                obs["detector_version"] = seg.get("detector") or nudenet_detector
                conf = float(seg.get("confidence") or 0)
                if fam == "siglip" and conf >= 1.0:
                    conf -= 1.0  # stored SigLIP scores are offset by +1 to rank below NudeNet
                obs["confidence"] = round(min(1.0, max(0.0, conf)), 4)
            add(obs_id("detect", title_key, fam, seg["start_ms"], seg["end_ms"]), obs)

        claim = match_claim(seg, claims) if claims else None
        if claim is None:
            continue
        cid = claim["claim_id"]
        if fp == db.FP_MARKED and (fam or seg.get("source") == "hayaa"):
            add(obs_id("reject", title_key, cid), {"kind": "reject", "claim_id": cid})
        corrected = _labels(seg.get("corrected_labels") or "")
        if corrected and (fam in ("nudenet", "siglip") or seg.get("source") == "hayaa"):
            add(obs_id("label", title_key, cid, *sorted(corrected)),
                {"kind": "label", "claim_id": cid, "labels": corrected, "severity": severity_for_labels(",".join(corrected))})

    if job.get("status") == "done" and (job.get("scan_version") or 0) > 0 and not machine_found:
        add(obs_id("clean", title_key, job.get("scan_version"), job.get("finished_at") or ""),
            {"kind": "clean", "detector": "nudenet", "detector_version": nudenet_detector, "scan_id": scan_id})

    for c in claims:
        cid = c["claim_id"]
        if overrides.get((title_key, cid)) == "keep":
            add(obs_id("confirm", title_key, cid), {"kind": "confirm", "claim_id": cid})
        if c.get("verify_state") in ("hit", "miss") and c.get("verify_family"):
            obs = {"kind": f"verify_{c['verify_state']}", "claim_id": cid, "detector": c["verify_family"],
                   "detector_version": c.get("verify_detector") or nudenet_detector}
            if c["verify_family"] == "siglip":
                obs["detector_version"] = "siglip-base-224"
            if c.get("verify_conf") is not None:
                obs["confidence"] = round(min(1.0, max(0.0, float(c["verify_conf"]))), 4)
            add(obs_id("verify", title_key, cid, c["verify_state"]), obs)
    return out


def plan_push(desired: dict[str, dict], ledger: list[dict], title_key: str
              ) -> tuple[list[dict], list[tuple[str, str, str, str]], list[str]]:
    """Diff one title's desired observations against what was sent.

    Returns (observations to push, ledger rows to record, obs ids being retracted). Hayaa ignores
    a repeated id, so an assertion made again after a retraction gets the next generation id.
    """
    live = {r["base_id"]: r for r in ledger if r["live"] and r["kind"] != "retract"}
    generations: dict[str, int] = {}
    for r in ledger:
        generations[r["base_id"]] = generations.get(r["base_id"], 0) + 1
    push: list[dict] = []
    rows: list[tuple[str, str, str, str]] = []
    for base, obs in desired.items():
        if base in live:
            continue
        gen = generations.get(base, 0)
        oid = base if gen == 0 else f"{base}-{gen}"
        push.append({"id": oid, **obs})
        rows.append((oid, base, title_key, obs["kind"]))
    retract = sorted(r["obs_id"] for base, r in live.items() if base not in desired)
    for i in range(0, len(retract), OBS_PER_PUSH):
        chunk = retract[i:i + OBS_PER_PUSH]
        rid = obs_id("retract", *chunk)
        push.append({"id": rid, "title": title_key, "kind": "retract", "retracts": chunk})
        rows.append((rid, rid, title_key, "retract"))
    return push, rows, retract


# ── Incoming: validate and decide ──────────────────────────────────────────────

def parse_view(view: Any, runtime_ms: int | None) -> list[dict] | None:
    """Validate a pulled title view; return its claims in ms, or None to drop the whole view."""
    if not isinstance(view, dict) or not isinstance(view.get("claims"), list) or len(view["claims"]) > 500:
        return None
    out = []
    for c in view["claims"]:
        try:
            start, end = float(c["start"]), float(c["end"])
            if not 0 <= start < end or (runtime_ms and end * 1000 > runtime_ms + 1000):
                return None
            labels = [str(lbl) for lbl in c.get("labels") or []]
            if c["status"] not in ("candidate", "accepted", "disputed", "rejected") or \
                    c.get("basis") not in ("machine", "human", "both") or \
                    any(lbl not in _KNOWN_LABELS for lbl in labels) or \
                    c.get("category", "nudity") not in CATEGORIES or \
                    c.get("severity", "high") not in ("low", "medium", "high"):
                return None
            machine = {str(k): float(v) for k, v in (c.get("machine") or {}).items()}
            out.append({
                "claim_id": str(c["claim_id"]), "start_ms": int(start * 1000), "end_ms": int(end * 1000),
                "status": c["status"], "basis": c["basis"], "labels": ",".join(labels),
                "category": c.get("category", "nudity"), "severity": c.get("severity", "high"),
                "machine": json.dumps(machine, sort_keys=True), "supporters": int(c.get("supporters", 0)),
                "rejecters": int(c.get("rejecters", 0)),
            })
        except (KeyError, TypeError, ValueError):
            return None
    return out


def threshold_ok(claim: dict, config) -> bool:
    """A machine-only claim acts only if a detector's best confidence passes this instance's threshold.

    Thresholds are a preference and stay local, so the same shared claim can apply on one
    instance and not on another. Human-backed claims and subtitle claims bypass this.
    """
    if claim["basis"] != "machine":
        return True
    machine = json.loads(claim.get("machine") or "{}")
    labels = [lbl for lbl in (claim.get("labels") or "").split(",") if lbl]
    for fam, conf in machine.items():
        if fam == "subtitles":
            return True
        if fam == "nudenet" and any(conf >= config.threshold_for(lbl) for lbl in labels or [""]):
            return True
        if fam == "siglip" and any(conf >= config.siglip_threshold_for(lbl) for lbl in labels or [""]):
            return True
    return not machine


def decide(claim: dict, override: str | None, preset: str, thresholds_pass: bool) -> str:
    """Apply matrix for a claim no local segment covers: 'apply', 'verify' (wait for the re-check),
    'hold' (review list) or 'skip'. The user's verdict always wins."""
    if override == "delete":
        return "skip"
    if override == "keep":
        return "apply"
    status, v = claim["status"], claim.get("verify_state") or ""
    if status == "rejected":
        return "skip"
    human = claim["basis"] in ("human", "both")
    if preset == "verify_first":
        return "apply" if v == "hit" else ("verify" if v in ("", "pending") else "hold")
    if not human and not thresholds_pass:
        return "skip"
    if status == "accepted":
        machine = json.loads(claim.get("machine") or "{}")
        same_detector_only = set(machine) == {claim.get("verify_family")}
        if v == "miss" and not human and same_detector_only:
            return "hold"  # the local detector should have reproduced it and did not
        return "apply"
    if status == "candidate":
        if v == "hit" or preset == "cautious":
            return "apply"
        return "verify" if v in ("", "pending") else "hold"
    # disputed: applies unless the local false-positive model objects to the verified frame
    if v == "hit" and claim.get("verify_fp_drop"):
        return "hold"
    return "apply"


def needs_verify(claim: dict) -> bool:
    """Frames can be re-checked for nudity claims; subtitle (language) claims cannot."""
    return claim["status"] != "rejected" and claim.get("category", "nudity") != "language"


# ── Apply ──────────────────────────────────────────────────────────────────────

async def get_preset() -> str:
    preset = await db.get_setting("hayaa_preset", "balanced")
    return preset if preset in PRESETS else "balanced"


async def apply_titles(plex_guids: list[str], config=None) -> dict[str, int]:
    """Bring segments in line with the staged claims of these titles; returns counts by outcome."""
    from .config import Config  # import here: config imports database at module load

    config = config or await Config.load()
    preset = await get_preset()
    claims_all = await db.get_hayaa_claims(plex_guids)
    by_guid: dict[str, list[dict]] = {}
    for c in claims_all:
        by_guid.setdefault(c["plex_guid"], []).append(c)
    segs_all = await db.get_segments_for_hayaa(plex_guids)
    segs_by_guid: dict[str, list[dict]] = {}
    for s in segs_all:
        segs_by_guid.setdefault(s["plex_guid"], []).append(s)
    keys = {c["title_key"] for c in claims_all}
    overrides = await db.get_hayaa_overrides(sorted(keys)) if keys else {}

    counts = {"applied": 0, "removed": 0, "hidden": 0, "restored": 0, "held": 0, "linked": 0}
    changed: set[str] = set()
    for guid in plex_guids:
        claims = by_guid.get(guid, [])
        segs = segs_by_guid.get(guid, [])
        own = [s for s in segs if s.get("source") != "hayaa"]
        pulled = {s["hayaa_claim_id"]: s for s in segs if s.get("source") == "hayaa" and s.get("hayaa_claim_id")}
        links: list[tuple[str, int]] = []
        title = next((s.get("title") for s in segs if s.get("title")), "")
        claim_ids = {c["claim_id"] for c in claims}

        for c in claims:
            tk, cid = c["title_key"], c["claim_id"]
            ov = overrides.get((tk, cid))
            local = [s for s in own if overlaps(s["start_ms"], s["end_ms"], c["start_ms"], c["end_ms"])]
            if local:
                decision = "local"
                for s in local:
                    if s.get("hayaa_claim_id") != cid:
                        links.append((cid, s["id"]))
                        counts["linked"] += 1
                if cid in pulled and (pulled[cid].get("false_positive") or 0) != db.FP_MARKED:
                    await segment_files.delete_segment(pulled[cid]["id"])
                    counts["removed"] += 1
                    changed.add(guid)
                hide = c["status"] == "rejected" and preset != "cautious" and ov != "restore" and \
                    (c["severity"] in ("low", "medium") or c.get("verify_fp_drop"))
                ids_on = [s["id"] for s in local if (s.get("false_positive") or 0) == 0 and s.get("source") in FAMILY_OF_SOURCE]
                ids_hidden = [s["id"] for s in local if s.get("false_positive") == db.FP_COMMUNITY]
                if hide and ids_on:
                    await db.set_segments_false_positive(ids_on, db.FP_COMMUNITY)
                    counts["hidden"] += len(ids_on)
                    changed.add(guid)
                elif not hide and ids_hidden:
                    await db.set_segments_false_positive(ids_hidden, 0)
                    counts["restored"] += len(ids_hidden)
                    changed.add(guid)
                if c["status"] == "rejected" and c["severity"] == "high" and not hide and ids_on:
                    decision = "ask"  # high severity: kept until the local model or the user agrees
            else:
                decision = decide(c, ov, preset, threshold_ok(c, config))
                existing = pulled.get(cid)
                if decision == "apply" and existing is None:
                    labels = c.get("labels") or ""
                    machine = json.loads(c.get("machine") or "{}")
                    language = c.get("category") == "language"
                    seg_id = await db.insert_segment(
                        plex_guid=guid, title=title or await _title_of(guid), start_ms=c["start_ms"],
                        end_ms=c["end_ms"], confidence=max(machine.values(), default=0.0),
                        thumbnail_path=c.get("verify_thumb") or None, labels=labels,
                        category=c.get("category") or "nudity", severity=c.get("severity") or "high",
                        action="mute" if language else "skip", channel="audio" if language else "both",
                        source="hayaa", detector="hayaa",
                    )
                    links.append((cid, seg_id))
                    counts["applied"] += 1
                    changed.add(guid)
                elif decision != "apply" and existing is not None and \
                        (existing.get("false_positive") or 0) != db.FP_MARKED:
                    await segment_files.delete_segment(existing["id"])
                    counts["removed"] += 1
                    changed.add(guid)
                if decision in ("hold", "verify"):
                    counts["held"] += 1
            if c.get("decision") != decision:
                await db.update_hayaa_claim(c["title_key"], cid, decision=decision)
            if needs_verify(c) and not c.get("verify_state") and decision != "local":
                await db.update_hayaa_claim(c["title_key"], cid, verify_state="pending")
            elif not needs_verify(c) and not c.get("verify_state"):
                await db.update_hayaa_claim(c["title_key"], cid, verify_state="na")

        # A pulled segment whose claim is gone from the view (merged, purged) goes too.
        for cid, s in pulled.items():
            if cid not in claim_ids and (s.get("false_positive") or 0) != db.FP_MARKED:
                await segment_files.delete_segment(s["id"])
                counts["removed"] += 1
                changed.add(guid)
        await db.set_segment_hayaa_claims(links)

    for guid in changed:
        events.publish("segments_changed", guid=guid)
        plex_summary.refresh_soon(guid)
    return counts


async def _title_of(guid: str) -> str:
    job = await db.get_scan_job_by_guid(guid)
    return (job or {}).get("title") or ""


# ── User verdicts on pulled segments ───────────────────────────────────────────

async def on_segment_deleted(seg: dict) -> None:
    """A user deleted a pulled segment: remember it so later pulls never bring it back."""
    if seg.get("source") == "hayaa" and seg.get("hayaa_claim_id"):
        key = await db.get_hayaa_title_key(seg["plex_guid"])
        if key:
            await db.set_hayaa_override(key, seg["hayaa_claim_id"], "delete")


async def on_false_positive_cleared(seg: dict) -> None:
    """A user restored a segment Hayaa's community had hidden: keep it shown from now on."""
    if seg.get("false_positive") == db.FP_COMMUNITY and seg.get("hayaa_claim_id"):
        key = await db.get_hayaa_title_key(seg["plex_guid"])
        if key:
            await db.set_hayaa_override(key, seg["hayaa_claim_id"], "restore")


async def review(title_key: str, claim_id: str, keep: bool) -> dict[str, int]:
    """Keep (confirm) or dismiss a held claim from the review list, then apply its title."""
    await db.set_hayaa_override(title_key, claim_id, "keep" if keep else "delete")
    claims = [c for c in await db.get_hayaa_claims() if c["title_key"] == title_key and c["claim_id"] == claim_id]
    if not claims:
        return {}
    return await apply_titles([claims[0]["plex_guid"]])


# ── State, quota and the sync run ──────────────────────────────────────────────

async def _get_json(key: str, default: Any) -> Any:
    try:
        return json.loads(await db.get_setting(key, "") or "null") or default
    except ValueError:
        return default


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _quota() -> dict[str, Any]:
    q = await _get_json("hayaa_quota", {})
    if q.get("day") != _utc_day():
        q = {"day": _utc_day(), "pushes": 0, "pulls": 0}
    return q


async def _spend(kind: str) -> None:
    q = await _quota()
    q[kind] = q.get(kind, 0) + 1
    await db.set_setting("hayaa_quota", json.dumps(q))


def _allowance(q: dict[str, Any], kind: str, manual: bool) -> int:
    limit = PUSHES_PER_DAY if kind == "pushes" else PULLS_PER_DAY
    cap = limit if manual else int(limit * AUTO_BUDGET)
    return max(0, cap - q.get(kind, 0))


_lock = asyncio.Lock()
_state: dict[str, Any] = {"running": False, "phase": ""}


def running() -> bool:
    return _lock.locked()


async def _nudenet_detector() -> str:
    model = await db.get_setting("nudenet_model", "320n") or "320n"
    custom = bool(await db.get_setting("nudenet_model_path", ""))
    return f"nudenet-{model}" + ("-custom" if custom else "")


async def _content_keys(titles: list[dict]) -> dict[str, dict]:
    """Content key -> title, for titles whose file is reachable now (others wait for a later sync)."""
    out: dict[str, dict] = {}
    for t in titles:
        path = t["file_path"]
        try:
            if not await asyncio.to_thread(os.path.isfile, path):
                continue
            key = await hayaa.content_key_for(path)
        except (OSError, ValueError) as exc:
            logger.debug("No content key for %s: %s", path, exc)
            continue
        out[key] = t
    return out


async def run_sync(manual: bool) -> dict[str, Any]:
    """One sync: describe new files, push what changed, pull changed consensus, apply it.

    Raises HayaaBusy (after recording the pause) and NotLinked; other per-request failures end
    the run with what was done so far. Only one sync runs at a time.
    """
    if _lock.locked():
        raise hayaa.HayaaError("A sync is already running")
    async with _lock:
        paused_until = float(await db.get_setting("hayaa_paused_until", "0") or 0)
        if paused_until > time.time():
            raise hayaa.HayaaBusy(int(paused_until - time.time()))
        result: dict[str, Any] = {"pushed": 0, "retracted": 0, "described": 0, "pulled": 0,
                                  "changed": 0, "proved": 0, "deferred": {},
                                  "skipped_quota": False, "error": ""}
        challenges: dict[str, list[int]] = {}
        try:
            _state["phase"] = "identifying files"
            titles = await db.get_hayaa_sync_titles()
            by_key = await _content_keys(titles)
            await db.upsert_hayaa_titles([(k, t["plex_guid"]) for k, t in by_key.items()])
            known = await db.get_hayaa_titles()

            _state["phase"] = "describing files"
            result["described"] = await _describe(by_key, known, manual, result)

            _state["phase"] = "pushing"
            await _push(by_key, manual, result, challenges)
            await _prove(by_key, challenges, result)

            _state["phase"] = "pulling"
            changed_guids = await _pull(by_key, known, manual, result, challenges)
            await _prove(by_key, challenges, result)  # these unlock on the next sync

            _state["phase"] = "applying"
            guids = sorted(set(changed_guids) | {c["plex_guid"] for c in await db.get_hayaa_claims()})
            result.update(await apply_titles(guids))
            await _check_disagreement()
        except hayaa.HayaaBusy as exc:
            pause = exc.retry_after or DEFAULT_PAUSE_S
            await db.set_setting("hayaa_paused_until", str(time.time() + pause))
            result["error"] = f"Hayaa is busy; paused for {pause // 60} min"
            raise
        except hayaa.HayaaError as exc:
            result["error"] = str(exc)
            if isinstance(exc, hayaa.NotLinked):
                raise
        finally:
            _state["phase"] = ""
            result["finished_at"] = time.time()
            result["manual"] = manual
            await db.set_setting("hayaa_last_sync", str(time.time()))
            await db.set_setting("hayaa_last_result", json.dumps(result))
            events.publish("hayaa_sync", phase="done")
        logger.info("Hayaa sync: %s", {k: v for k, v in result.items() if v})
        return result


async def _describe(by_key: dict[str, dict], known: dict[str, dict], manual: bool, result: dict) -> int:
    """Send media descriptors (release key, runtime) for files Hayaa has not been told about."""
    todo = [k for k in by_key if not known.get(k, {}).get("described")]
    sent = 0
    for i in range(0, len(todo), MEDIA_PER_PUSH):
        if _allowance(await _quota(), "pushes", manual) <= 0:
            result["skipped_quota"] = True
            break
        chunk = todo[i:i + MEDIA_PER_PUSH]
        media = []
        for k in chunk:
            t = by_key[k]
            try:
                size = (await asyncio.to_thread(os.stat, t["file_path"])).st_size
            except OSError:
                continue
            d: dict[str, Any] = {"title": k, "release_key": hayaa.release_key(Path(t["file_path"]).name, size)}
            if t.get("duration_ms"):
                d["runtime"] = round(t["duration_ms"] / 1000.0, 3)
            media.append(d)
        if not media:
            continue
        await _spend("pushes")
        await hayaa.signed_post("/api/v1/media", {"media": media})
        await db.mark_hayaa_described([m["title"] for m in media])
        sent += len(media)
    return sent


async def _push(by_key: dict[str, dict], manual: bool, result: dict,
                challenges: dict[str, list[int]]) -> None:
    guids = [t["plex_guid"] for t in by_key.values()]
    segs: dict[str, list[dict]] = {}
    for s in await db.get_segments_for_hayaa(guids):
        segs.setdefault(s["plex_guid"], []).append(s)
    claims: dict[str, list[dict]] = {}
    for c in await db.get_hayaa_claims(guids):
        claims.setdefault(c["plex_guid"], []).append(c)
    keys = list(by_key)
    overrides = await db.get_hayaa_overrides(keys)
    ledger: dict[str, list[dict]] = {}
    for r in await db.get_hayaa_sent(keys):
        ledger.setdefault(r["title_key"], []).append(r)
    detector = await _nudenet_detector()

    batch: list[dict] = []
    rows: list[tuple[str, str, str, str]] = []
    retracted: list[str] = []

    async def flush() -> bool:
        nonlocal batch, rows, retracted
        if not batch:
            return True
        if _allowance(await _quota(), "pushes", manual) <= 0:
            result["skipped_quota"] = True
            return False
        await _spend("pushes")
        reply = await hayaa.signed_post("/api/v1/push", {"observations": batch})
        challenges.update(reply.get("challenges") or {})
        await db.record_hayaa_sent(rows, retracted)
        result["pushed"] += sum(1 for o in batch if o["kind"] != "retract")
        result["retracted"] += len(retracted)
        batch, rows, retracted = [], [], []
        return True

    for key, t in by_key.items():
        want = desired_observations(key, t, segs.get(t["plex_guid"], []), claims.get(t["plex_guid"], []),
                                    overrides, detector)
        obs, new_rows, gone = plan_push(want, ledger.get(key, []), key)
        if not obs:
            continue
        if len(batch) + len(obs) > OBS_PER_PUSH and not await flush():
            return
        batch += obs
        rows += new_rows
        retracted += gone
    await flush()


async def _prove(by_key: dict[str, dict], challenges: dict[str, list[int]], result: dict) -> None:
    """Answer Hayaa's possession challenges from the files themselves (ABUSE_RESISTANCE §2).

    Each answer reads one 64 KB chunk; a proofs-only push costs no daily push quota on Hayaa.
    """
    proofs: dict[str, dict[str, str]] = {}
    for key, positions in challenges.items():
        t = by_key.get(key)
        if t is None:
            continue
        try:
            proofs[key] = await asyncio.to_thread(hayaa.proofs_for_file, Path(t["file_path"]), list(positions))
        except (OSError, ValueError) as exc:
            logger.debug("Cannot prove %s: %s", t.get("title"), exc)
    keys = list(proofs)
    for i in range(0, len(keys), PULL_BATCH):
        chunk = {k: proofs[k] for k in keys[i:i + PULL_BATCH]}
        await hayaa.signed_post("/api/v1/push", {"observations": [], "proofs": chunk})
        result["proved"] += len(chunk)
    challenges.clear()


async def _pull(by_key: dict[str, dict], known: dict[str, dict], manual: bool, result: dict,
                challenges: dict[str, list[int]]) -> list[str]:
    """Pull changed views, partly released and least recently pulled titles first; stage them.

    Hayaa holds titles back (not scanned here, unproven, over the viewing budget, account too
    new) and releases long titles a window at a time; held titles and their challenges are
    reported in `result` and `challenges`. Returns changed guids.
    """
    partial = set(json.loads(await db.get_setting("hayaa_partial", "[]") or "[]"))
    order = sorted(by_key, key=lambda k: (k not in partial, known.get(k, {}).get("pulled_at") or ""))
    changed: list[str] = []
    versions: dict[str, str] = {}
    for i in range(0, len(order), PULL_BATCH):
        if _allowance(await _quota(), "pulls", manual) <= 0:
            result["skipped_quota"] = True
            break
        chunk = order[i:i + PULL_BATCH]
        have = {k: (known.get(k, {}).get("version") or "")[:16] for k in chunk}
        await _spend("pulls")
        reply = await hayaa.signed_post("/api/v1/pull", {"have": have})
        result["pulled"] += len(chunk)
        for key, view in (reply.get("views") or {}).items():
            if key not in by_key:
                continue
            t = by_key[key]
            claims = parse_view(view, t.get("duration_ms") or None)
            if claims is None:
                logger.warning("Dropped an invalid Hayaa view for %s", t.get("title"))
                continue
            await db.replace_hayaa_claims(key, t["plex_guid"], claims)
            versions[key] = str(view.get("version") or "")
            changed.append(t["plex_guid"])
            if view.get("complete") is False:
                partial.add(key)
            else:
                partial.discard(key)
        for key, why in (reply.get("deferred") or {}).items():
            reason = str((why or {}).get("reason") or "unknown")
            result["deferred"][reason] = result["deferred"].get(reason, 0) + 1
        challenges.update(reply.get("challenges") or {})
        # Unchanged titles are still marked as pulled, so the rotation moves on.
        await db.set_hayaa_title_versions({k: versions.get(k, have[k]) for k in chunk})
    await db.set_setting("hayaa_partial", json.dumps(sorted(partial & set(by_key))))
    result["changed"] = len(changed)
    return changed


async def _check_disagreement() -> None:
    """Switch to verify_first when local re-checks keep contradicting machine-only accepted claims."""
    claims = [c for c in await db.get_hayaa_claims()
              if c["status"] == "accepted" and c["basis"] == "machine" and c.get("verify_state") in ("hit", "miss")]
    if len(claims) < DISAGREE_MIN_SAMPLES:
        return
    misses = sum(1 for c in claims if c["verify_state"] == "miss")
    if misses / len(claims) > DISAGREE_SWITCH and await get_preset() != "verify_first":
        await db.set_setting("hayaa_preset", "verify_first")
        await db.set_setting("hayaa_preset_auto_switched", "1")
        logger.warning("Hayaa: local re-checks disagree with %d of %d shared claims; switched to Verify first",
                       misses, len(claims))
        events.publish("hayaa_sync", phase="preset_switched")


async def sync_status() -> dict[str, Any]:
    """Sync settings and the last run for the UI."""
    q = await _quota()
    return {
        "running": running(),
        "phase": _state["phase"],
        "auto_sync": await db.get_setting("hayaa_auto_sync", "0") == "1",
        "interval_h": int(await db.get_setting("hayaa_sync_interval_h", "12") or 12),
        "preset": await get_preset(),
        "preset_auto_switched": await db.get_setting("hayaa_preset_auto_switched", "0") == "1",
        "last_sync": float(await db.get_setting("hayaa_last_sync", "0") or 0) or None,
        "last_result": await _get_json("hayaa_last_result", None),
        "paused_until": float(await db.get_setting("hayaa_paused_until", "0") or 0) or None,
        "quota": {"pushes": q.get("pushes", 0), "pulls": q.get("pulls", 0),
                  "pushes_limit": PUSHES_PER_DAY, "pulls_limit": PULLS_PER_DAY},
        "held": await db.count_hayaa_claims(decision="hold") + await db.count_hayaa_claims(decision="verify"),
        "verify_pending": await db.count_hayaa_claims(verify_state="pending"),
    }


# ── Automatic sync ─────────────────────────────────────────────────────────────

AUTO_CHECK_S = 300


async def auto_sync_due(now: float | None = None) -> bool:
    """True when automatic sync is on, linked, not paused and the interval has passed."""
    now = now if now is not None else time.time()
    if await db.get_setting("hayaa_auto_sync", "0") != "1" or not hayaa.is_linked() or running():
        return False
    settings = await db.get_all_settings()
    if not settings.get("hayaa_api_url") or not settings.get("hayaa_auth_url"):
        return False
    if float(settings.get("hayaa_paused_until") or 0) > now:
        return False
    interval_s = max(1, int(settings.get("hayaa_sync_interval_h") or 12)) * 3600
    return now - float(settings.get("hayaa_last_sync") or 0) >= interval_s


async def hayaa_sync_loop() -> None:
    """Background job: run an automatic sync when one is due (opt-in, §5.7)."""
    while True:
        try:
            if await auto_sync_due():
                await run_sync(manual=False)
        except asyncio.CancelledError:
            raise
        except hayaa.HayaaError as exc:
            logger.info("Automatic Hayaa sync stopped: %s", exc)
        except Exception:
            logger.exception("Automatic Hayaa sync failed")
        await asyncio.sleep(AUTO_CHECK_S)
