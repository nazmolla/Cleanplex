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
    # Per-label overrides of confidence_threshold / siglip_threshold; a label with no
    # entry inherits the global value (#120).
    label_thresholds: dict[str, float] = field(default_factory=dict)
    siglip_label_thresholds: dict[str, float] = field(default_factory=dict)
    skip_buffer_ms: int = 3000
    pre_buffer_ms: int = 3000
    post_buffer_ms: int = 3000
    scan_step_ms: int = 5000
    scan_workers: int = 2
    nudenet_model: str = "320n"
    nudenet_model_path: str = ""
    segment_gap_ms: int = 12000
    segment_min_hits: int = 1
    # Re-sample each flagged window this finely for its thumbnail and bounds; 0 turns it off (#133).
    refine_step_ms: int = 500
    # SigLIP fallback for frames NudeNet did not flag (siglip_detector.py).
    siglip_enabled: bool = False
    siglip_threshold: float = 0.3
    siglip_min_hits: int = 2
    # Share of kept segments the feedback-trained verifier may drop (5/10/20); 0 = off (#165).
    verifier_loss_pct: int = 0
    # Do not run SigLIP after a title's Plex credits marker (#121).
    ignore_credits: bool = True
    # onnxruntime threads per session; 0 derives it from scan_workers (#123).
    inference_threads: int = 0
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
    # Also render hover previews whenever the scan queue is empty, not only inside the hover window (#152).
    hover_when_idle: bool = True
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

        def _thresholds(val: str) -> dict[str, float]:
            try:
                parsed = json.loads(val)
            except Exception:
                return {}
            if not isinstance(parsed, dict):
                return {}
            out: dict[str, float] = {}
            for label, value in parsed.items():
                try:
                    out[str(label)] = float(value)
                except (TypeError, ValueError):
                    continue
            return out

        return cls(
            plex_url=s.get("plex_url", ""),
            plex_token=s.get("plex_token", ""),
            poll_interval=int(s.get("poll_interval", "5")),
            confidence_threshold=float(s.get("confidence_threshold", "0.6")),
            label_thresholds=_thresholds(s.get("label_thresholds", "{}")),
            siglip_label_thresholds=_thresholds(s.get("siglip_label_thresholds", "{}")),
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
            refine_step_ms=max(0, int(s.get("refine_step_ms", "500"))),
            siglip_enabled=s.get("siglip_enabled", "0") == "1",
            siglip_threshold=float(s.get("siglip_threshold", "0.3")),
            siglip_min_hits=max(1, int(s.get("siglip_min_hits", "2"))),
            verifier_loss_pct=int(s.get("verifier_loss_pct", "0")),
            ignore_credits=s.get("ignore_credits", "1") == "1",
            inference_threads=max(0, int(s.get("inference_threads", "0"))),
            hw_decode=s.get("hw_decode", "1") == "1",
            scan_ratings=_labels(s.get("scan_ratings", "[]")),
            scan_labels=_labels(s.get("scan_labels", "[]")),
            scan_window_start=_time(s.get("scan_window_start", "23:00")),
            scan_window_end=_time(s.get("scan_window_end", "06:00")),
            hover_previews_enabled=s.get("hover_previews_enabled", "1") == "1",
            hover_when_idle=s.get("hover_when_idle", "1") == "1",
            hover_window_start=_time(s.get("hover_window_start", "00:00")),
            hover_window_end=_time(s.get("hover_window_end", "07:00")),
            log_level=s.get("log_level", "INFO"),
        )

    def threshold_for(self, label: str) -> float:
        """NudeNet confidence a *label* must reach: its override, else the global threshold."""
        return self.label_thresholds.get(label, self.confidence_threshold)

    def siglip_threshold_for(self, label: str) -> float:
        """SigLIP score a hit tagged *label* must reach: its override, else the global one."""
        return self.siglip_label_thresholds.get(label, self.siglip_threshold)

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
