"""Parser and exporter for Kodi/MPlayer EDL (edit decision list) files.

Format is three whitespace-separated columns::

    30.5        45.0        0
    00:01:30.5  00:01:45.0  1

Actions: 0 cut, 1 mute, 2 scene marker, 3 commercial break. Times may be float
seconds, HH:MM:SS.sss, or `#frames` — frames are rejected because the frame rate
is not known at import time.

EDL carries no category information, so imported cuts land in `other` and
commercial breaks in `commercial`. Note that Plex does not read EDL outside its
DVR path: this is interchange with Kodi, Jellyfin and comskip, not with Plex.
"""

from __future__ import annotations

from ._common import ParseError, to_ms, validate_segment

SOURCE = "edl"

# Scene markers are navigation aids rather than filters, so they are parsed and
# then dropped rather than turned into skips.
_SCENE_MARKER = 2

_ACTIONS = {
    0: {"action": "skip", "category": "other"},
    1: {"action": "mute", "category": "language", "channel": "audio"},
    3: {"action": "skip", "category": "commercial"},
}


def parse(text: str) -> list[dict]:
    """Parse an EDL body into normalized segments."""
    segments: list[dict] = []
    saw_row = False

    for lineno, raw in enumerate(text.replace("\r\n", "\n").split("\n"), start=1):
        line = raw.strip()
        if not line or line.startswith("#") and " " not in line:
            continue

        fields = line.split()
        if len(fields) < 3:
            raise ParseError(
                f"Line {lineno}: expected 'start end action', got {line!r}"
            )

        try:
            action_code = int(fields[2])
        except ValueError as exc:
            raise ParseError(
                f"Line {lineno}: action must be 0, 1, 2 or 3, got {fields[2]!r}"
            ) from exc

        start_ms = to_ms(fields[0])
        end_ms = to_ms(fields[1])
        saw_row = True

        if action_code == _SCENE_MARKER:
            continue
        mapping = _ACTIONS.get(action_code)
        if mapping is None:
            raise ParseError(
                f"Line {lineno}: unknown EDL action {action_code}; expected 0, 1, 2 or 3"
            )

        segments.append(validate_segment({"start_ms": start_ms, "end_ms": end_ms, **mapping}))

    if not saw_row:
        raise ParseError("No EDL rows found. Expected lines of the form '30.5 45.0 0'.")

    segments.sort(key=lambda s: s["start_ms"])
    return segments
