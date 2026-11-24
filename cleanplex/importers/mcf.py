"""Parser and exporter for MovieContentFilter `.mcf` files (spec 1.1.0).

A WebVTT subset::

    WEBVTT MovieContentFilter 1.1.0

    NOTE
    TITLE Ozymandias
    YEAR 2013

    NOTE
    START 00:00:04.020
    END 01:24:00.100

    00:06:14.000 --> 00:06:17.581
    gambling=medium # Some comment
    drugs=high=video

Each cue carries one or more `category=severity[=channel]` lines, so one cue can
produce several segments sharing its time range. The START/END block declares
which cut of the work the timings were authored against, which is what makes a
runtime sanity check possible before accepting an import.
"""

from __future__ import annotations

import re

from ._common import CATEGORIES, ParseError, to_ms, validate_segment

SOURCE = "mcf"

_HEADER_RE = re.compile(r"^WEBVTT\s+MovieContentFilter\s+([\d.]+)", re.IGNORECASE)
_CUE_RE = re.compile(r"^\s*([\d:.,]+)\s*-+>\s*([\d:.,]+)\s*$")

# The spec's ~130 categories are hierarchical: every specific term belongs to one
# of the nine top-level groups. We store the group, so an unknown specific term
# still lands somewhere sensible rather than being dropped.
_SPECIFIC_TO_GROUP = {
    "advertbreak": "commercial", "consumerism": "commercial", "productplacement": "commercial",
    "adultism": "discrimination", "antisemitism": "discrimination", "genderism": "discrimination",
    "homophobia": "discrimination", "misandry": "discrimination", "misogyny": "discrimination",
    "racism": "discrimination", "sexism": "discrimination", "supremacism": "discrimination",
    "transphobia": "discrimination", "xenophobia": "discrimination",
    "idiocy": "dispensable", "tedious": "dispensable",
    "alcohol": "drugs", "antipsychotics": "drugs", "cigarettes": "drugs", "depressants": "drugs",
    "gambling": "drugs", "hallucinogens": "drugs", "stimulants": "drugs",
    "blasphemy": "language", "namecalling": "language", "sexualdialogue": "language",
    "swearing": "language", "vulgarity": "language",
    "barebuttocks": "nudity", "exposedgenitalia": "nudity", "fullnudity": "nudity",
    "toplessness": "nudity",
    "adultery": "sex", "analsex": "sex", "coitus": "sex", "kissing": "sex",
    "masturbation": "sex", "objectification": "sex", "oralsex": "sex",
    "premaritalsex": "sex", "promiscuity": "sex", "prostitution": "sex",
    "choking": "violence", "crueltytoanimals": "violence", "culturalviolence": "violence",
    "desecration": "violence", "emotionalviolence": "violence", "kicking": "violence",
    "massacre": "violence", "murder": "violence", "punching": "violence", "rape": "violence",
    "slapping": "violence", "slavery": "violence", "stabbing": "violence", "torture": "violence",
    "warfare": "violence", "weapons": "violence",
}

# Everything in the "fear" group; listed separately because the group name itself
# is also a valid category and the list is long.
_FEAR_TERMS = {
    "accident", "acrophobia", "aliens", "arachnophobia", "astraphobia", "aviophobia",
    "chemophobia", "claustrophobia", "coulrophobia", "cynophobia", "death", "dentophobia",
    "emetophobia", "enochlophobia", "explosion", "fire", "gerascophobia", "ghosts", "grave",
    "hemophobia", "hylophobia", "melissophobia", "misophonia", "musophobia", "mysophobia",
    "nosocomephobia", "nyctophobia", "siderodromephobia", "siderodromophobia",
    "thalassophobia", "vampires",
}


def _to_group(category: str) -> str:
    key = category.strip().lower()
    if key in CATEGORIES:
        return key
    if key in _FEAR_TERMS:
        return "fear"
    return _SPECIFIC_TO_GROUP.get(key, "other")


def parse(text: str) -> list[dict]:
    """Parse an `.mcf` body into normalized segments."""
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = body.split("\n")

    if not lines or not _HEADER_RE.match(lines[0].strip()):
        raise ParseError(
            "Missing MCF header. The first line must be "
            "'WEBVTT MovieContentFilter <version>'."
        )

    segments: list[dict] = []
    index = 0
    while index < len(lines):
        match = _CUE_RE.match(lines[index])
        if not match:
            index += 1
            continue

        start_ms = to_ms(match.group(1))
        end_ms = to_ms(match.group(2))
        index += 1

        # Every non-blank line under the timestamp is one category for this cue.
        while index < len(lines) and lines[index].strip():
            payload = lines[index].split("#", 1)[0].strip()
            index += 1
            if not payload:
                continue
            parts = [p.strip() for p in payload.split("=")]
            seg = {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "category": _to_group(parts[0]),
                "severity": parts[1].lower() if len(parts) > 1 else "high",
                "channel": parts[2].lower() if len(parts) > 2 else "both",
                "labels": parts[0],
            }
            # MCF describes what to remove, not how. Audio-only cues are muted;
            # anything else is skipped.
            seg["action"] = "mute" if seg["channel"] == "audio" else "skip"
            segments.append(validate_segment(seg))

    if not segments:
        raise ParseError("No cues found in the MCF file.")

    segments.sort(key=lambda s: s["start_ms"])
    return segments
