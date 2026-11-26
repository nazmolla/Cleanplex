"""Pulling consensus views and staging claims from Hayaa (#234)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from .. import database as db
from .. import hayaa
from ..logger import get_logger
from .models import (
    PULL_BATCH,
    PULL_JOB_POLLS,
    HayaaTitleRecord,
    PullPending,
    SyncResult,
    SyncTitle,
    parse_view,
)
from .quota import QuotaTracker

logger = get_logger(__name__)


class HayaaPuller:
    """Polls Hayaa's change feed, pulls updated consensus views and stages claims."""

    def __init__(
        self,
        quota: QuotaTracker,
        poll_limit_fn: Callable[[], int] | None = None,
    ) -> None:
        self._quota = quota
        self._poll_limit_fn = poll_limit_fn or (lambda: PULL_JOB_POLLS)

    async def changed_keys(
        self,
        by_key: dict[str, SyncTitle],
        known: dict[str, HayaaTitleRecord],
    ) -> tuple[list[str], str]:
        """Titles worth pulling and the feed cursor to save once the pull finishes."""
        cursor = await db.get_setting("hayaa_feed_cursor", "") or ""
        changed: set[str] = set()
        new_cursor = cursor
        while True:
            _, d = await hayaa.signed_get("/api/v1/changes", {"since": new_cursor})
            changed.update(d.get("titles") or [])
            new_cursor = str(d.get("cursor") or new_cursor)
            if not d.get("more"):
                break
        if not cursor:
            return list(by_key), new_cursor
        retry = set(json.loads(await db.get_setting("hayaa_partial", "[]") or "[]")) | set(
            json.loads(await db.get_setting("hayaa_deferred", "[]") or "[]")
        )
        keys = [
            k
            for k in by_key
            if k in changed or k in retry or not (known.get(k) and known[k].pulled_at)
        ]
        return keys, new_cursor

    async def _pull_batch(self, have: dict[str, str]) -> dict[str, Any]:
        """One pull; a large one is a job on Hayaa, collected when ready (docs/ASYNC_SYNC.md)."""
        reply = await hayaa.signed_post("/api/v1/pull", {"have": have})
        limit = self._poll_limit_fn()
        for _ in range(limit):
            if "job" not in reply or "views" in reply:
                break
            await asyncio.sleep(float(reply.get("retry_after", 2)))
            _, reply = await hayaa.signed_get("/api/v1/pull/result", {"job": str(reply["job"])})
        if "error" in reply and "views" not in reply:
            raise hayaa.HayaaError(str(reply["error"]))
        if "views" not in reply:
            raise PullPending()
        return reply

    async def pull(
        self,
        by_key: dict[str, SyncTitle],
        known: dict[str, HayaaTitleRecord],
        manual: bool,
        result: SyncResult,
        challenges: dict[str, list[int]],
    ) -> list[str]:
        """Pull changed views, partly released and least recently pulled titles first; stage them."""
        to_pull, next_cursor = await self.changed_keys(by_key, known)
        partial = set(json.loads(await db.get_setting("hayaa_partial", "[]") or "[]"))
        order = sorted(
            to_pull,
            key=lambda k: (k not in partial, (known[k].pulled_at if k in known else "") or ""),
        )
        changed: list[str] = []
        versions: dict[str, str] = {}
        deferred_keys: set[str] = set()
        for i in range(0, len(order), PULL_BATCH):
            if self._quota.allowance(await self._quota.current(), "pulls", manual) <= 0:
                result.skipped_quota = True
                break
            chunk = order[i:i + PULL_BATCH]
            have = {k: (known[k].version if k in known else "")[:16] for k in chunk}
            await self._quota.spend("pulls")
            try:
                reply = await self._pull_batch(have)
            except PullPending:
                result.pull_pending = len(order) - i
                deferred_keys.update(order[i:])  # retried next sync, whatever the feed says
                break
            result.pulled += len(chunk)
            for key, view in (reply.get("views") or {}).items():
                if key not in by_key:
                    continue
                t = by_key[key]
                claims = parse_view(view, t.duration_ms or None)
                if claims is None:
                    logger.warning("Dropped an invalid Hayaa view for %s", t.title)
                    continue
                await db.replace_hayaa_claims(
                    key, t.plex_guid, [c.to_db_dict() for c in claims], version_id=t.version_id
                )
                versions[key] = str(view.get("version") or "")
                changed.append(t.plex_guid)
                if view.get("complete") is False:
                    partial.add(key)
                else:
                    partial.discard(key)
            for key, why in (reply.get("deferred") or {}).items():
                # Retried next sync only when time alone can release it (budget, scan time, window).
                # Possession is proven by matching another holder, so that waits for the change
                # feed to say someone else contributed.
                if str((why or {}).get("reason")) not in ("prove_possession", "not_contributed"):
                    deferred_keys.add(key)
                reason = str((why or {}).get("reason") or "unknown")
                result.deferred[reason] = result.deferred.get(reason, 0) + 1
            challenges.update(reply.get("challenges") or {})
            # Unchanged titles are still marked as pulled, so the rotation moves on.
            await db.set_hayaa_title_versions({k: versions.get(k, have[k]) for k in chunk})
        await db.set_setting("hayaa_partial", json.dumps(sorted(partial)))
        await db.set_setting("hayaa_deferred", json.dumps(sorted(deferred_keys)))
        await db.set_setting("hayaa_feed_cursor", next_cursor)
        result.changed = len(changed)
        return changed
