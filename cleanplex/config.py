"""Runtime configuration backed by the SQLite settings table."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import time

from . import database as db


@dataclass
class Config:
    plex_url: str = ""
    plex_token: str = ""
    poll_interval: int = 5
    confidence_threshold: float = 0.6
    skip_buffer_ms: int = 3000
    pre_buffer_ms: int = 3000
    post_buffer_ms: int = 3000
    scan_step_ms: int = 5000
    scan_workers: int = 2
    nudenet_model: str = "320n"
    nudenet_model_path: str = ""
    segment_gap_ms: int = 12000
    segment_min_hits: int = 1
    # SigLIP fallback for frames NudeNet did not flag (siglip_detector.py).
    siglip_enabled: bool = False
    siglip_threshold: float = 0.3
    siglip_min_hits: int = 2
    # ffmpeg -hwaccel auto for scans; falls back to software per file (#107).
    hw_decode: bool = True
    scan_ratings: list[str] = field(default_factory=list)  # empty = scan all ratings
    scan_labels: list[str] = field(default_factory=lambda: [
        "FEMALE_BREAST_EXPOSED",
        "FEMALE_GENITALIA_EXPOSED",
        "MALE_GENITALIA_EXPOSED",
        "ANUS_EXPOSED",
        "BUTTOCKS_EXPOSED",
    ])
    scan_window_start: time = field(default_factory=lambda: time(23, 0))
    scan_window_end: time = field(default_factory=lambda: time(6, 0))
    hover_previews_enabled: bool = True
    hover_window_start: time = field(default_factory=lambda: time(0, 0))
    hover_window_end: time = field(default_factory=lambda: time(7, 0))
    log_level: str = "INFO"

    @classmethod
    async def load(cls) -> "Config":
        s = await db.get_all_settings()

        def _time(val: str) -> time:
            h, m = val.split(":")
            return time(int(h), int(m))

        def _labels(val: str) -> list[str]:
            try:
                parsed = json.loads(val)
                if isinstance(parsed, list):
                    return [str(x) for x in parsed if isinstance(x, str)]
            except Exception:
                pass
            # Return empty list if parsing fails - no scanning until labels are configured
            return []

        return cls(
            plex_url=s.get("plex_url", ""),
            plex_token=s.get("plex_token", ""),
            poll_interval=int(s.get("poll_interval", "5")),
            confidence_threshold=float(s.get("confidence_threshold", "0.6")),
            skip_buffer_ms=int(s.get("skip_buffer_ms", "3000")),
            # Fall back to skip_buffer_ms so a settings row written before the split
            # still governs both edges rather than silently reverting to the default.
            pre_buffer_ms=int(s.get("pre_buffer_ms") or s.get("skip_buffer_ms", "3000")),
            post_buffer_ms=int(s.get("post_buffer_ms") or s.get("skip_buffer_ms", "3000")),
            scan_step_ms=int(s.get("scan_step_ms", "5000")),
            scan_workers=max(1, int(s.get("scan_workers", "2"))),
            nudenet_model=s.get("nudenet_model", "320n"),
            nudenet_model_path=s.get("nudenet_model_path", ""),
            segment_gap_ms=int(s.get("segment_gap_ms", "12000")),
            segment_min_hits=int(s.get("segment_min_hits", "1")),
            siglip_enabled=s.get("siglip_enabled", "0") == "1",
            siglip_threshold=float(s.get("siglip_threshold", "0.3")),
            siglip_min_hits=max(1, int(s.get("siglip_min_hits", "2"))),
            hw_decode=s.get("hw_decode", "1") == "1",
            scan_ratings=_labels(s.get("scan_ratings", "[]")),
            scan_labels=_labels(s.get("scan_labels", "[]")),
            scan_window_start=_time(s.get("scan_window_start", "23:00")),
            scan_window_end=_time(s.get("scan_window_end", "06:00")),
            hover_previews_enabled=s.get("hover_previews_enabled", "1") == "1",
            hover_window_start=_time(s.get("hover_window_start", "00:00")),
            hover_window_end=_time(s.get("hover_window_end", "07:00")),
            log_level=s.get("log_level", "INFO"),
        )

    def is_configured(self) -> bool:
        return bool(self.plex_url and self.plex_token)

    def is_scan_window(self) -> bool:
        """Return True if current local time is within the scan window."""
        return _in_window(self.scan_window_start, self.scan_window_end)

    def is_hover_window(self) -> bool:
        """Return True if current local time is within the hover preview window."""
        return _in_window(self.hover_window_start, self.hover_window_end)


def _in_window(start: time, end: time) -> bool:
    """Return True if the current local time is within [start, end]."""
    from datetime import datetime
    now = datetime.now().time().replace(second=0, microsecond=0)
    if start <= end:
        return start <= now <= end
    # Window wraps midnight (e.g. 23:00 – 06:00)
    return now >= start or now <= end
