"""Status snapshot builder for the Hayaa sync UI (#234)."""

from __future__ import annotations

import json
from typing import Any

from .. import database as db
from .models import PRESETS, PULLS_PER_DAY, PUSHES_PER_DAY
from .quota import QuotaTracker


class SyncStatusReader:
    """Reads sync settings, daily quota and pending counts for the UI."""

    def __init__(self, quota: QuotaTracker) -> None:
        self._quota = quota

    @staticmethod
    async def _get_json(key: str, default: Any) -> Any:
        try:
            return json.loads(await db.get_setting(key, "") or "null") or default
        except ValueError:
            return default

    async def read(self, *, running: bool, phase: str) -> dict[str, Any]:
        q = await self._quota.current()
        preset = await db.get_setting("hayaa_preset", "balanced")
        if preset not in PRESETS:
            preset = "balanced"
        return {
            "running": running,
            "phase": phase,
            "auto_sync": await db.get_setting("hayaa_auto_sync", "0") == "1",
            "interval_h": int(await db.get_setting("hayaa_sync_interval_h", "12") or 12),
            "preset": preset,
            "preset_auto_switched": await db.get_setting("hayaa_preset_auto_switched", "0") == "1",
            "last_sync": float(await db.get_setting("hayaa_last_sync", "0") or 0) or None,
            "last_result": await self._get_json("hayaa_last_result", None),
            "paused_until": float(await db.get_setting("hayaa_paused_until", "0") or 0) or None,
            "quota": {
                "pushes": q.get("pushes", 0),
                "pulls": q.get("pulls", 0),
                "pushes_limit": PUSHES_PER_DAY,
                "pulls_limit": PULLS_PER_DAY,
            },
            "held": await db.count_hayaa_claims(decision="hold") + await db.count_hayaa_claims(decision="verify"),
            "pending_titles": await db.count_hayaa_pending(),
            "verify_pending": await db.count_hayaa_claims(verify_state="pending"),
        }
