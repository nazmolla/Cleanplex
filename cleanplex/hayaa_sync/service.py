"""Sync run orchestrator owning runtime lock, phase and background task (#234)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from .. import database as db
from .. import events, hayaa
from ..logger import get_logger
from .applier import ClaimApplier
from .content_keys import ContentKeyStore
from .models import AUTO_CHECK_S, DEFAULT_PAUSE_S, SyncResult, SyncTitle, HayaaTitleRecord
from .planner import PushPlanner
from .puller import HayaaPuller
from .pusher import HayaaPusher
from .quota import QuotaTracker
from .status import SyncStatusReader

logger = get_logger(__name__)


class HayaaSyncService:
    """Coordinates Hayaa sync runs and owns the mutable sync state (no module globals).

    Change tracking via SQLite triggers (created in ``database.init_db``):
    Only versions whose ``title_versions.hayaa_sync_state != 'synced'`` are pushed. Before pushing,
    ``db.mark_hayaa_sending`` flips those rows to ``'sending'``; after Hayaa accepts the push
    (202), ``db.mark_hayaa_synced`` flips rows that are *still* ``'sending'`` to ``'synced'``.
    If any local change happens concurrently (or later), these triggers reset
    ``hayaa_sync_state`` back to ``'pending'``:

    - ``hayaa_pending_scan``: ``AFTER UPDATE OF status, finished_at, scan_version ON title_versions``
      when ``NEW.status = 'done'`` (new or redone scan).
    - ``hayaa_pending_seg_ins``: ``AFTER INSERT ON segments`` when ``NEW.source != 'hayaa'``
      (own detection or subtitle scan added; pulled segments never trigger a push).
    - ``hayaa_pending_seg_upd``: ``AFTER UPDATE OF false_positive, labels, start_ms, end_ms ON segments``
      when ``NEW.source != 'hayaa' OR NEW.false_positive != OLD.false_positive``
      (user edited/marked own segment or marked a pulled segment false positive; label
      backfills from ``fill_hayaa_segment_labels`` do not trigger).
    - ``hayaa_pending_seg_del``: ``AFTER DELETE ON segments`` when ``OLD.source != 'hayaa'``
      (own segment deleted -> retract; deleting a pulled segment never triggers).
    - ``hayaa_pending_feedback``: ``AFTER INSERT ON segment_feedback``
      (user false-positive or relabel verdict recorded).
    - ``hayaa_pending_override_ins`` / ``hayaa_pending_override_upd`` / ``hayaa_pending_override_del``:
      ``ON hayaa_overrides`` matched by ``content_key = title_key`` (user keep/delete/restore verdict).
    - ``hayaa_pending_verify``: ``AFTER UPDATE OF verify_state ON hayaa_claims``
      (local re-check finished with hit/miss/na).
    """

    def __init__(
        self,
        *,
        content_keys: ContentKeyStore | None = None,
        planner: PushPlanner | None = None,
        quota: QuotaTracker | None = None,
        pusher: HayaaPusher | None = None,
        puller: HayaaPuller | None = None,
        applier: ClaimApplier | None = None,
        status_reader: SyncStatusReader | None = None,
        poll_limit_fn=None,
    ) -> None:
        self._lock = asyncio.Lock()
        self._phase = ""
        self._sync_task: asyncio.Task[Any] | None = None
        self.content_keys = content_keys or ContentKeyStore()
        self.planner = planner or PushPlanner()
        self.quota = quota or QuotaTracker()
        self.pusher = pusher or HayaaPusher(self.planner, self.quota)
        self.puller = puller or HayaaPuller(self.quota, poll_limit_fn=poll_limit_fn)
        self.applier = applier or ClaimApplier()
        self.status_reader = status_reader or SyncStatusReader(self.quota)

    def running(self) -> bool:
        return self._lock.locked()

    @property
    def phase(self) -> str:
        return self._phase

    async def sync_status(self) -> dict[str, Any]:
        return await self.status_reader.read(running=self.running(), phase=self._phase)

    async def start_manual_sync(self, force: bool = False) -> dict[str, Any]:
        """Start a manual sync task in the background and return the immediate status."""
        if self.running() or (self._sync_task is not None and not self._sync_task.done()):
            raise hayaa.HayaaError("A sync is already running")

        async def _run() -> None:
            try:
                await self.run_sync(manual=True, force=force)
            except hayaa.HayaaError:
                pass  # recorded in hayaa_last_result, which the status shows

        self._sync_task = asyncio.create_task(_run())
        await asyncio.sleep(0)  # let it take the lock, so the status below says running
        return await self.sync_status()

    async def run_sync(self, manual: bool, force: bool = False) -> dict[str, Any]:
        """One sync: describe new files, push changed titles, pull consensus, apply it."""
        if self._lock.locked():
            raise hayaa.HayaaError("A sync is already running")
        async with self._lock:
            paused_until = float(await db.get_setting("hayaa_paused_until", "0") or 0)
            if paused_until > time.time():
                raise hayaa.HayaaBusy(int(paused_until - time.time()))
            result = SyncResult(manual=manual)
            challenges: dict[str, list[int]] = {}
            try:
                self._phase = "identifying files"
                if force:
                    await db.mark_all_hayaa_pending()
                    await db.set_setting("hayaa_feed_cursor", "")
                titles = [SyncTitle.from_row(r) for r in await db.get_hayaa_sync_titles()]
                by_key = await self.content_keys.resolve_live(titles)
                # Removed titles whose key was kept are pushed but never pulled or proved: the file
                # is gone, and Hayaa only asks for the file before it hands segments out (#228).
                gone = await self.content_keys.load_removed(set(by_key))
                all_by_key = {**gone, **by_key}
                await db.upsert_hayaa_titles([(k, t.plex_guid, t.version_id) for k, t in all_by_key.items()])
                known = {k: HayaaTitleRecord.from_row(r) for k, r in (await db.get_hayaa_titles()).items()}

                self._phase = "describing files"
                result.described = await self.pusher.describe(all_by_key, known)

                self._phase = "pushing"
                # Relies on the SQLite triggers documented on HayaaSyncService to reset any
                # version touched mid-push back to 'pending' so mark_hayaa_synced won't clear it.
                pending = {k: t for k, t in by_key.items() if t.hayaa_sync_state != "synced"}
                pending_targets: list[int] | list[str] = (
                    [t.version_id for t in pending.values() if t.version_id]
                    if all(t.version_id for t in pending.values())
                    else [t.plex_guid for t in pending.values()]
                )
                await db.mark_hayaa_sending(pending_targets)
                if await self.pusher.push({**gone, **pending}, manual, result):
                    await db.mark_hayaa_synced(pending_targets)

                self._phase = "pulling"
                changed_guids = await self.puller.pull(by_key, known, manual, result, challenges)
                await self.pusher.prove(by_key, challenges, result)  # these unlock on the next sync

                self._phase = "applying"
                guids = sorted(set(changed_guids) | {c["plex_guid"] for c in await db.get_hayaa_claims()})
                result.apply = await self.applier.apply_titles(guids)
                await self.applier.check_disagreement()
            except hayaa.HayaaBusy as exc:
                pause = exc.retry_after or DEFAULT_PAUSE_S
                await db.set_setting("hayaa_paused_until", str(time.time() + pause))
                until = time.strftime("%H:%M", time.localtime(time.time() + pause))
                result.error = (
                    f"Daily upload limit reached; continues after {until}"
                    if exc.quota
                    else f"Hayaa is busy; paused for {pause // 60} min"
                )
                raise
            except hayaa.HayaaError as exc:
                result.error = str(exc)
                if isinstance(exc, hayaa.NotLinked):
                    raise
            finally:
                self._phase = ""
                result.finished_at = time.time()
                payload = result.to_dict()
                await db.set_setting("hayaa_last_sync", str(result.finished_at))
                await db.set_setting("hayaa_last_result", json.dumps(payload))
                events.publish("hayaa_sync", phase="done")
            logger.info("Hayaa sync: %s", {k: v for k, v in payload.items() if v})
            return payload

    async def auto_sync_due(self, now: float | None = None) -> bool:
        """True when automatic sync is on, linked, not paused and the interval has passed."""
        now = now if now is not None else time.time()
        if await db.get_setting("hayaa_auto_sync", "0") != "1" or not hayaa.is_linked() or self.running():
            return False
        settings = await db.get_all_settings()
        if float(settings.get("hayaa_paused_until") or 0) > now:
            return False
        interval_s = max(1, int(settings.get("hayaa_sync_interval_h") or 12)) * 3600
        return now - float(settings.get("hayaa_last_sync") or 0) >= interval_s

    async def sync_loop(self) -> None:
        """Background job: run an automatic sync when one is due (opt-in, §5.7)."""
        while True:
            try:
                if await self.auto_sync_due():
                    await self.run_sync(manual=False)
            except asyncio.CancelledError:
                raise
            except hayaa.HayaaError as exc:
                logger.info("Automatic Hayaa sync stopped: %s", exc)
            except Exception:
                logger.exception("Automatic Hayaa sync failed")
            await asyncio.sleep(AUTO_CHECK_S)
