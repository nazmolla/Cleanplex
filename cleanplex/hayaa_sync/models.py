"""Typed records, constants and pure protocol helpers for Hayaa sync (#234)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from ..importers._common import ALL_SEVERITIES, CATEGORIES, DETECTOR_LABELS

# Segment sources that are this instance's own detections. Imported skip files never leave the
# instance (§5.4), and pulled segments ('hayaa') are not ours to assert.
FAMILY_OF_SOURCE: dict[str, str] = {"scanner": "nudenet", "siglip": "siglip", "subtitles": "subtitles"}
FAMILY_CATEGORY: dict[str, str] = {"nudenet": "nudity", "siglip": "nudity", "subtitles": "language"}
# Same overlap tolerance Hayaa clusters with, so a local segment and a claim it supports match.
TOLERANCE_MS = 2000

# Hayaa's per-user daily limits (hayaa.rules.config.LimitsConfig) and request caps.
PUSHES_PER_DAY = 20
PULLS_PER_DAY = 60
# Automatic sync keeps a fifth of each daily quota free for a manual sync.
AUTO_BUDGET = 0.8
# Hayaa allows 2,000 per request, but writes each item to table storage; smaller batches keep
# every request well inside HTTP_TIMEOUT_S.
OBS_PER_PUSH = 500
MEDIA_PER_PUSH = 200
PULL_BATCH = 500
PULL_JOB_POLLS = 300  # about ten minutes of retry_after waits for a large pull job
# Hayaa refuses a body over 256 KB, whatever the item count; batches stay well under it.
BODY_BUDGET = 192 * 1024
DEFAULT_PAUSE_S = 3600  # when a 429/503 carries no Retry-After

PRESETS = ("balanced", "cautious", "verify_first")
# Verify results on machine-only accepted claims: above this share of misses the service is
# not trusted for this instance and the preset drops to verify_first (CONFLICTS §7.2).
DISAGREE_SWITCH = 0.10
DISAGREE_MIN_SAMPLES = 20
AUTO_CHECK_S = 300

KNOWN_LABELS = set(DETECTOR_LABELS)


class PullPending(Exception):
    """Hayaa is still preparing a large pull; its titles come on the next sync."""


@dataclass(slots=True)
class SyncTitle:
    """One local (or recently removed) single-file version eligible for Hayaa sync."""

    plex_guid: str
    version_id: int = 0
    title: str = ""
    file_path: str = ""
    duration_ms: int = 0
    status: str = ""
    scan_version: int = 0
    finished_at: str = ""
    content_key: str = ""
    content_size: int = 0
    content_mtime: int = 0
    hayaa_sync_state: str = "pending"
    removed: bool = False

    @classmethod
    def from_row(cls, row: dict[str, Any], *, removed: bool = False) -> SyncTitle:
        return cls(
            plex_guid=str(row["plex_guid"]),
            version_id=int(row.get("version_id") or 0),
            title=str(row.get("title") or ""),
            file_path=str(row.get("file_path") or ""),
            duration_ms=int(row.get("duration_ms") or 0),
            status=str(row.get("status") or ""),
            scan_version=int(row.get("scan_version") or 0),
            finished_at=str(row.get("finished_at") or ""),
            content_key=str(row.get("content_key") or ""),
            content_size=int(row.get("content_size") or row.get("size") or 0),
            content_mtime=int(row.get("content_mtime") or 0),
            hayaa_sync_state=str(row.get("hayaa_sync_state") or "pending"),
            removed=removed,
        )


@dataclass(slots=True)
class Observation:
    """One assertion this instance desires to publish for a title (before assigning a generation id)."""

    base_id: str
    title_key: str
    kind: str
    body: dict[str, Any]

    def to_wire(self, obs_id_value: str) -> dict[str, Any]:
        return {"id": obs_id_value, "title": self.title_key, "kind": self.kind, **self.body}


@dataclass(slots=True)
class LedgerEntry:
    """A row from ``hayaa_sent`` recording a previously sent observation."""

    obs_id: str
    base_id: str
    title_key: str
    kind: str
    live: bool

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> LedgerEntry:
        return cls(
            obs_id=str(row["obs_id"]),
            base_id=str(row["base_id"]),
            title_key=str(row["title_key"]),
            kind=str(row["kind"]),
            live=bool(row.get("live", 1)),
        )


@dataclass(slots=True)
class PushPlan:
    """Diff between desired observations and the sent-ledger for one title."""

    observations: list[dict[str, Any]]
    ledger_rows: list[tuple[str, str, str, str]]
    retracted_ids: list[str]


@dataclass(slots=True)
class HayaaTitleRecord:
    """Known Hayaa title metadata from ``hayaa_titles``."""

    title_key: str
    plex_guid: str
    version_id: int = 0
    version: str = ""
    described: bool = False
    pulled_at: str = ""

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> HayaaTitleRecord:
        return cls(
            title_key=str(row["title_key"]),
            plex_guid=str(row["plex_guid"]),
            version_id=int(row.get("version_id") or 0),
            version=str(row.get("version") or ""),
            described=bool(row.get("described")),
            pulled_at=str(row.get("pulled_at") or ""),
        )


@dataclass(slots=True)
class HayaaClaim:
    """A consensus claim staged in ``hayaa_claims`` (times in ms)."""

    title_key: str
    claim_id: str
    plex_guid: str
    start_ms: int
    end_ms: int
    status: str
    basis: str
    version_id: int = 0
    labels: str = ""
    category: str = "nudity"
    severity: str = "high"
    machine: dict[str, float] = field(default_factory=dict)
    supporters: int = 0
    rejecters: int = 0
    verify_state: str = ""
    verify_conf: float | None = None
    verify_family: str = ""
    verify_fp_drop: int = 0
    verify_thumb: str = ""
    verify_labels: str = ""
    decision: str = ""

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> HayaaClaim:
        raw_machine = row.get("machine")
        if isinstance(raw_machine, dict):
            machine = {str(k): float(v) for k, v in raw_machine.items()}
        else:
            try:
                parsed = json.loads(raw_machine or "{}")
                machine = {str(k): float(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                machine = {}
        return cls(
            title_key=str(row.get("title_key") or ""),
            claim_id=str(row.get("claim_id") or ""),
            plex_guid=str(row.get("plex_guid") or ""),
            version_id=int(row.get("version_id") or 0),
            start_ms=int(row.get("start_ms") or 0),
            end_ms=int(row.get("end_ms") or 0),
            status=str(row.get("status") or ""),
            basis=str(row.get("basis") or ""),
            labels=str(row.get("labels") or ""),
            category=str(row.get("category") or "nudity"),
            severity=str(row.get("severity") or "high"),
            machine=machine,
            supporters=int(row.get("supporters") or 0),
            rejecters=int(row.get("rejecters") or 0),
            verify_state=str(row.get("verify_state") or ""),
            verify_conf=float(row["verify_conf"]) if row.get("verify_conf") is not None else None,
            verify_family=str(row.get("verify_family") or ""),
            verify_fp_drop=int(row.get("verify_fp_drop") or 0),
            verify_thumb=str(row.get("verify_thumb") or ""),
            verify_labels=str(row.get("verify_labels") or ""),
            decision=str(row.get("decision") or ""),
        )

    def to_db_dict(self) -> dict[str, Any]:
        """Shape expected by ``db.replace_hayaa_claims``."""
        return {
            "claim_id": self.claim_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "status": self.status,
            "basis": self.basis,
            "labels": self.labels,
            "category": self.category,
            "severity": self.severity,
            "machine": json.dumps(self.machine, sort_keys=True),
            "supporters": self.supporters,
            "rejecters": self.rejecters,
        }


@dataclass(slots=True)
class ApplyCounts:
    """Outcome counts from applying staged claims to local segments."""

    applied: int = 0
    removed: int = 0
    hidden: int = 0
    restored: int = 0
    held: int = 0
    linked: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "applied": self.applied,
            "removed": self.removed,
            "hidden": self.hidden,
            "restored": self.restored,
            "held": self.held,
            "linked": self.linked,
        }


@dataclass(slots=True)
class SyncResult:
    """Summary of one Hayaa sync run."""

    pushed: int = 0
    retracted: int = 0
    described: int = 0
    pulled: int = 0
    changed: int = 0
    proved: int = 0
    deferred: dict[str, int] = field(default_factory=dict)
    skipped_quota: bool = False
    error: str = ""
    pull_pending: int | None = None
    apply: ApplyCounts | None = None
    finished_at: float = 0.0
    manual: bool = False

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "pushed": self.pushed,
            "retracted": self.retracted,
            "described": self.described,
            "pulled": self.pulled,
            "changed": self.changed,
            "proved": self.proved,
            "deferred": dict(self.deferred),
            "skipped_quota": self.skipped_quota,
            "error": self.error,
            "finished_at": self.finished_at,
            "manual": self.manual,
        }
        if self.pull_pending is not None:
            data["pull_pending"] = self.pull_pending
        if self.apply is not None:
            data.update(self.apply.to_dict())
        return data


def obs_id(*parts: object) -> str:
    """Stable observation id (32 hex, inside Hayaa's 8-64 char id rule)."""
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:32]


def overlaps(a_start: int, a_end: int, b_start: int, b_end: int, tol: int = TOLERANCE_MS) -> bool:
    return a_start < b_end + tol and b_start < a_end + tol


def item_json_size(item: dict[str, Any]) -> int:
    return len(json.dumps(item, separators=(",", ":"))) + 1


def chunk_by_size(items: list[dict[str, Any]], budget: int = BODY_BUDGET) -> list[list[dict[str, Any]]]:
    """Split items into runs whose JSON stays under *budget*."""
    out: list[list[dict[str, Any]]] = [[]]
    size = 0
    for it in items:
        n = item_json_size(it)
        if out[-1] and size + n > budget:
            out.append([])
            size = 0
        out[-1].append(it)
        size += n
    return [p for p in out if p]


def parse_view(view: Any, runtime_ms: int | None) -> list[HayaaClaim] | None:
    """Validate a pulled title view; return its claims in ms, or None to drop the whole view."""
    if not isinstance(view, dict) or not isinstance(view.get("claims"), list) or len(view["claims"]) > 500:
        return None
    out: list[HayaaClaim] = []
    for c in view["claims"]:
        try:
            start, end = float(c["start"]), float(c["end"])
            if not 0 <= start < end or (runtime_ms and end * 1000 > runtime_ms + 1000):
                return None
            labels = [str(lbl) for lbl in c.get("labels") or []]
            if (
                c["status"] not in ("candidate", "accepted", "disputed", "rejected")
                or c.get("basis") not in ("machine", "human", "both")
                or any(lbl not in KNOWN_LABELS for lbl in labels)
                or c.get("category", "nudity") not in CATEGORIES
                or c.get("severity", "high") not in ALL_SEVERITIES
            ):
                return None
            machine = {str(k): float(v) for k, v in (c.get("machine") or {}).items()}
            out.append(HayaaClaim(
                title_key="",
                claim_id=str(c["claim_id"]),
                plex_guid="",
                start_ms=int(start * 1000),
                end_ms=int(end * 1000),
                status=c["status"],
                basis=c["basis"],
                labels=",".join(labels),
                category=c.get("category", "nudity"),
                severity=c.get("severity", "high"),
                machine=machine,
                supporters=int(c.get("supporters", 0)),
                rejecters=int(c.get("rejecters", 0)),
            ))
        except (KeyError, TypeError, ValueError):
            return None
    return out
