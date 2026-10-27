"""Subtitle-driven profanity detection: emits muted language segments.

Unlike the NudeNet scanner this needs no frames and no inference — it reads the
subtitle track and matches a wordlist, so a title costs seconds rather than
minutes. VideoSkip uses the same approach for its Exchange files, which is why
imported `.skp` profanity cues line up with what this produces.

Mute, not skip: a spoken word lasts under a second, and seeking around it would
be both jarring and imprecise.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from .logger import get_logger
from . import database as db
from . import events
from .frame_extractor import _FFMPEG_BIN, _FFPROBE_BIN

logger = get_logger(__name__)

# Padding around a subtitle cue. Subtitle timings lead the audio slightly and a
# word can run past its cue, so a small cushion each side is the difference
# between muting the word and muting the silence next to it.
PAD_MS = 150

# Hits closer together than this are merged: a burst of profanity would otherwise
# produce a stutter of separate mute/restore commands.
MERGE_GAP_MS = 1000

# Sidecar subtitle extensions, most preferred first.
_SIDECAR_SUFFIXES = (".srt", ".vtt")

DEFAULT_WORDLIST = [
    "arse", "arsehole", "ass", "asshole", "bastard", "bitch", "bollocks",
    "bullshit", "cock", "cunt", "damn", "dick", "dickhead", "dyke", "fag",
    "faggot", "fuck", "goddamn", "jackass", "jerkoff", "motherfucker", "nigga",
    "nigger", "piss", "prick", "pussy", "retard", "shit", "slut", "twat",
    "wanker", "whore",
]

# Matches a listed word plus optional doubled consonant and common suffixes, so
# "shitting" and "fucked" are caught while "class" is not mistaken for a stem.
_SUFFIX_PATTERN = r"(?:[a-z])?(?:s|es|d|ed|ing|er|ers|y)?"

_SRT_CUE_RE = re.compile(
    r"(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})\s*-+>\s*(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})\s*\n(.*?)(?=\n\s*\n|\Z)",
    re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>|\{[^}]+\}")


def _to_ms(value: str) -> int:
    hours, minutes, rest = value.replace(",", ".").split(":")
    return int(round((int(hours) * 3600 + int(minutes) * 60 + float(rest)) * 1000))


def build_pattern(words: list[str]) -> re.Pattern:
    """Compile a word-boundary regex for the given wordlist."""
    escaped = [re.escape(w.strip().lower()) for w in words if w.strip()]
    if not escaped:
        # An empty list must never compile to a pattern that matches everything.
        return re.compile(r"(?!x)x")
    return re.compile(rf"\b({'|'.join(escaped)}){_SUFFIX_PATTERN}\b", re.IGNORECASE)


def parse_subtitles(text: str) -> list[dict]:
    """Parse SRT or WebVTT text into {"start_ms", "end_ms", "text"} cues."""
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for match in _SRT_CUE_RE.finditer(body):
        content = _TAG_RE.sub("", match.group(3)).replace("\n", " ").strip()
        if content:
            cues.append({
                "start_ms": _to_ms(match.group(1)),
                "end_ms": _to_ms(match.group(2)),
                "text": content,
            })
    return cues


def find_hits(cues: list[dict], pattern: re.Pattern) -> list[dict]:
    """Return padded, merged language segments for cues containing listed words."""
    hits = []
    for cue in cues:
        match = pattern.search(cue["text"])
        if not match:
            continue
        hits.append({
            "start_ms": max(0, cue["start_ms"] - PAD_MS),
            "end_ms": cue["end_ms"] + PAD_MS,
            "category": "language",
            "severity": "high",
            "action": "mute",
            "channel": "audio",
            "labels": match.group(0).lower(),
            "confidence": 1.0,
        })

    merged: list[dict] = []
    for hit in sorted(hits, key=lambda h: h["start_ms"]):
        if merged and hit["start_ms"] - merged[-1]["end_ms"] <= MERGE_GAP_MS:
            merged[-1]["end_ms"] = max(merged[-1]["end_ms"], hit["end_ms"])
            # Keep every matched word so the segment explains itself in the UI.
            existing = merged[-1]["labels"].split(",")
            if hit["labels"] not in existing:
                merged[-1]["labels"] = ",".join(existing + [hit["labels"]])
        else:
            merged.append(hit)
    return merged


def find_sidecar_subtitle(media_path: str) -> Path | None:
    """Return a subtitle file sitting beside the media, if there is one."""
    media = Path(media_path)
    for suffix in _SIDECAR_SUFFIXES:
        candidate = media.with_suffix(suffix)
        if candidate.exists():
            return candidate
    return None


async def extract_embedded_subtitle(file_path: str, language: str = "eng") -> str | None:
    """Pull the first matching embedded subtitle track out as SRT text."""
    probe = await asyncio.create_subprocess_exec(
        _FFPROBE_BIN,
        "-loglevel", "error",
        "-select_streams", "s",
        "-show_entries", "stream=index:stream_tags=language",
        "-of", "json",
        file_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await probe.communicate()
    if probe.returncode != 0:
        return None

    try:
        streams = json.loads(out.decode("utf-8", errors="replace")).get("streams", [])
    except json.JSONDecodeError:
        return None
    if not streams:
        return None

    # Prefer the requested language, but any subtitle track beats none.
    chosen = next(
        (i for i, s in enumerate(streams) if (s.get("tags") or {}).get("language") == language),
        0,
    )

    proc = await asyncio.create_subprocess_exec(
        _FFMPEG_BIN,
        "-loglevel", "error",
        "-i", file_path,
        "-map", f"0:s:{chosen}",
        "-f", "srt",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    srt_out, _ = await proc.communicate()
    if proc.returncode != 0 or not srt_out:
        return None
    return srt_out.decode("utf-8", errors="replace")


async def load_wordlist() -> list[str]:
    """Return the configured profanity wordlist, falling back to the default."""
    raw = await db.get_setting("profanity_wordlist", "")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list) and parsed:
                return [str(w) for w in parsed]
        except json.JSONDecodeError:
            logger.warning("profanity_wordlist is not valid JSON — using the default list")
    return DEFAULT_WORDLIST


async def scan_title(plex_guid: str, title: str, file_path: str, language: str = "eng") -> int:
    """Scan one title's subtitles and store language segments. Returns the count."""
    sidecar = find_sidecar_subtitle(file_path)
    if sidecar is not None:
        text = sidecar.read_text(encoding="utf-8", errors="replace")
    else:
        text = await extract_embedded_subtitle(file_path, language)

    if not text:
        logger.info("No subtitles available for '%s' — nothing to scan", title)
        return 0

    pattern = build_pattern(await load_wordlist())
    segments = find_hits(parse_subtitles(text), pattern)
    # Stamp the track these timings came from: they do not hold for a dub.
    for seg in segments:
        seg["language"] = language
    if not segments:
        logger.info("No listed words found in subtitles for '%s'", title)
        return 0

    count = await db.insert_segments_bulk(plex_guid, title, segments, source="subtitles")
    events.publish("segments_changed", guid=plex_guid)
    logger.info("Found %d language segment(s) in '%s'", count, title)
    return count
