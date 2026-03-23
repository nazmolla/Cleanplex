"""Runtime configuration backed by the SQLite settings table."""

from __future__ import annotations

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
    scan_window_start: time = field(default_factory=lambda: time(23, 0))
    scan_window_end: time = field(default_factory=lambda: time(6, 0))
    log_level: str = "INFO"

    @classmethod
    async def load(cls) -> "Config":
        s = await db.get_all_settings()

        def _time(val: str) -> time:
            h, m = val.split(":")
            return time(int(h), int(m))

        return cls(
            plex_url=s.get("plex_url", ""),
            plex_token=s.get("plex_token", ""),
            poll_interval=int(s.get("poll_interval", "5")),
            confidence_threshold=float(s.get("confidence_threshold", "0.6")),
            skip_buffer_ms=int(s.get("skip_buffer_ms", "3000")),
            scan_window_start=_time(s.get("scan_window_start", "23:00")),
            scan_window_end=_time(s.get("scan_window_end", "06:00")),
            log_level=s.get("log_level", "INFO"),
        )

    def is_configured(self) -> bool:
        return bool(self.plex_url and self.plex_token)

    def is_scan_window(self) -> bool:
        """Return True if current local time is within the scan window."""
        from datetime import datetime
        now = datetime.now().time().replace(second=0, microsecond=0)
        start = self.scan_window_start
        end = self.scan_window_end
        if start <= end:
            return start <= now <= end
        # Window wraps midnight (e.g. 23:00 – 06:00)
        return now >= start or now <= end
