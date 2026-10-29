"""Shared helpers for skip file parsers: timestamps and the category vocabulary."""

from __future__ import annotations

import re

# MovieContentFilter 1.1.0 top-level categories. Parsers normalize onto these so
# user preferences and analytics work the same regardless of source format.
CATEGORIES = (
    "commercial",
    "discrimination",
    "dispensable",
    "drugs",
    "fear",
    "language",
    "nudity",
    "sex",
    "violence",
    "other",
)

SEVERITIES = ("low", "medium", "high")

# NudeNet's label vocabulary: the labels a scanner segment can carry, and what a user
# may relabel one to.
DETECTOR_LABELS = (
    "FEMALE_GENITALIA_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "MALE_BREAST_EXPOSED",
    "ANUS_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "FEET_EXPOSED",
    "BELLY_EXPOSED",
    "ARMPITS_EXPOSED",
    "FEMALE_GENITALIA_COVERED",
    "FEMALE_BREAST_COVERED",
    "MALE_BREAST_COVERED",
    "ANUS_COVERED",
    "BUTTOCKS_COVERED",
    "FEET_COVERED",
    "BELLY_COVERED",
    "ARMPITS_COVERED",
    "FACE_FEMALE",
    "FACE_MALE",
)

# Severity of a detected label, which sets what each viewer level skips: a segment
# fires when level + rank > 3, so level 3 skips everything, 2 skips medium and high
# (exposed nudity), 1 only high (genitalia and anus), 0 nothing (#126). Labels not
# listed (covered parts, belly, feet, faces) are low.
LABEL_SEVERITY = {
    "FEMALE_GENITALIA_EXPOSED": "high",
    "MALE_GENITALIA_EXPOSED": "high",
    "ANUS_EXPOSED": "high",
    "FEMALE_BREAST_EXPOSED": "medium",
    "BUTTOCKS_EXPOSED": "medium",
}
_SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3}


def severity_for_labels(labels: str, default: str = "high") -> str:
    """Return the highest severity among comma-separated *labels*; *default* if none.

    Used for scanner output only: imported segments keep the severity their author set.
    """
    found = [lbl.strip() for lbl in (labels or "").split(",") if lbl.strip()]
    if not found:
        return default
    return max((LABEL_SEVERITY.get(lbl, "low") for lbl in found), key=_SEVERITY_ORDER.__getitem__)
ACTIONS = ("skip", "mute", "blank", "blur", "fast")
CHANNELS = ("both", "video", "audio")

_TIME_RE = re.compile(r"^\d+(:\d{1,2}){0,2}([.,]\d+)?$")


class ParseError(ValueError):
    """Raised when a skip file cannot be understood. Message is user-facing."""


def to_ms(value: str) -> int:
    """Convert a skip-file timestamp to milliseconds.

    Accepts `H:MM:SS.ss`, `MM:SS.ss` and bare seconds, with `,` or `.` as the
    decimal separator. Hours are not zero-padded in VideoSkip output, and EDL
    files often use plain float seconds, so all three shapes are common.
    """
    raw = value.strip().replace(",", ".")
    if raw.startswith("#"):
        raise ParseError(
            f"Frame-based timing ({value}) is not supported: the frame rate is "
            "unknown at import time. Re-export the file with time-based timings."
        )
    if not _TIME_RE.match(raw):
        raise ParseError(f"Unrecognised timestamp: {value!r}")

    parts = raw.split(":")
    try:
        seconds = float(parts[-1])
        if len(parts) >= 2:
            seconds += int(parts[-2]) * 60
        if len(parts) == 3:
            seconds += int(parts[0]) * 3600
    except ValueError as exc:
        raise ParseError(f"Unrecognised timestamp: {value!r}") from exc
    return int(round(seconds * 1000))


def to_timestamp(ms: int) -> str:
    """Render milliseconds as `HH:MM:SS.mmm`, the form MCF requires."""
    total_seconds, millis = divmod(max(0, int(ms)), 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def validate_segment(seg: dict) -> dict:
    """Normalize one parsed segment, raising if the times make no sense."""
    if seg["end_ms"] < seg["start_ms"]:
        raise ParseError(
            f"Segment ends before it starts ({seg['start_ms']}ms → {seg['end_ms']}ms)"
        )
    seg.setdefault("category", "other")
    seg.setdefault("severity", "high")
    seg.setdefault("action", "skip")
    seg.setdefault("channel", "both")
    seg.setdefault("labels", "")
    if seg["category"] not in CATEGORIES:
        seg["category"] = "other"
    if seg["severity"] not in SEVERITIES:
        seg["severity"] = "high"
    if seg["channel"] not in CHANNELS:
        seg["channel"] = "both"
    return seg
