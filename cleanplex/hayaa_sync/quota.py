"""Daily quota tracking for automatic and manual Hayaa syncs (#234)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .. import database as db
from .models import AUTO_BUDGET, PULLS_PER_DAY, PUSHES_PER_DAY


class QuotaTracker:
    """Tracks daily Hayaa push/pull usage in settings."""

    @staticmethod
    def utc_day() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    async def current(self) -> dict[str, Any]:
        raw = await db.get_setting("hayaa_quota", "") or "null"
        try:
            q = json.loads(raw) or {}
        except ValueError:
            q = {}
        if q.get("day") != self.utc_day():
            q = {"day": self.utc_day(), "pushes": 0, "pulls": 0}
        return q

    async def spend(self, kind: str) -> None:
        q = await self.current()
        q[kind] = q.get(kind, 0) + 1
        await db.set_setting("hayaa_quota", json.dumps(q))

    @staticmethod
    def allowance(q: dict[str, Any], kind: str, manual: bool) -> int:
        """Requests left for this sync. Manual syncs defer to Hayaa's own limit."""
        if manual:
            return 1 << 30
        limit = PUSHES_PER_DAY if kind == "pushes" else PULLS_PER_DAY
        return max(0, int(limit * AUTO_BUDGET) - q.get(kind, 0))
