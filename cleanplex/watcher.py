"""Session watcher and library poller."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from datetime import datetime

from .logger import get_logger
from . import database as db
from . import events
from . import filter_engine
from . import importers
from .scanner import enqueue_many, enqueue_versions, rating_allowed, scanner_loop

logger = get_logger(__name__)

# Ring buffer of recent skip events for the live dashboard. The durable history
# lives in the skip_events table; this is just the hot cache.
skip_events: deque[dict] = deque(maxlen=50)

# Floor for adaptive polling, so a session sitting right before a segment cannot
# drive the poll rate high enough to hammer the Plex API.
MIN_POLL_INTERVAL_S = 1.0
# Every pass lists the whole Plex library, which almost never changes; 5 min is quick enough for new items (#162).
LIBRARY_POLL_INTERVAL_S = 300
_last_sessions_signature: tuple = ()


async def session_watcher_loop(get_config_fn, get_client_fn) -> None:
    """Poll Plex sessions every `poll_interval` seconds and fire skips."""
    global _last_sessions_signature
    while True:
        config = await get_config_fn()

        if not config.is_configured():
            await asyncio.sleep(10)
            continue

        sessions = []
        try:
            client = get_client_fn()
            sessions = await client.get_active_sessions()
            # Tell the UI when streams start, stop or move. Whole seconds, so a paused
            # stream stops producing events while a playing one ticks each poll.
            signature = tuple(sorted((s.session_key, s.position_ms // 1000) for s in sessions))
            if signature != _last_sessions_signature:
                _last_sessions_signature = signature
                events.publish("sessions_changed")

            # Drop tracking state for sessions that ended, restoring volume for any
            # that stopped mid-mute. Plex reuses session keys, so stale entries
            # would otherwise suppress skips for unrelated playback.
            await filter_engine.reap({s.session_key for s in sessions}, client)

            for session in sessions:
                await _remember_client_control(session)
                user_filter = await db.get_user_filter(session.user)
                # Default: filter enabled if no explicit record
                if user_filter is None or user_filter["enabled"]:
                    await _backstop(session, client)
                    await filter_engine.process(
                        session,
                        client,
                        config.pre_buffer_ms,
                        config.post_buffer_ms,
                        config.poll_interval * 1000,
                    )

                    # Log skip event if a skip just happened (detect by checking _recently_skipped)
                    sk = filter_engine._recently_skipped.get(session.session_key, 0)
                    if sk and sk > session.position_ms:
                        skip_events.appendleft({
                            "time": datetime.now().isoformat(timespec="seconds"),
                            "user": session.user,
                            "title": session.full_title,
                            "position_ms": session.position_ms,
                            "client": session.client_title,
                        })

        except Exception as exc:
            logger.warning("Session watcher error: %s", exc)
            sessions = []

        await asyncio.sleep(await _next_poll_delay(sessions, config))


# Plex playback session ids already stopped by the backstop, so each is stopped once.
_backstopped: set[str] = set()
# A stream gets this long to show up in the proxy's sessions before it counts as a bypass:
# the proxy records a session at transcode start, Plex reports it a poll later.
BACKSTOP_GRACE_S = 20
_first_seen: dict[str, float] = {}


async def _backstop(session, client) -> None:
    """Stop a filtered viewer's stream of a flagged title that never passed the stream proxy (#184).

    Only once the front door is installed (enforcement_enabled): from then on every
    client reaches Plex through the proxy, so a stream it never saw came another way
    (relay, DLNA, a misconfigured firewall) and would play unfiltered.
    """
    if await db.get_setting("enforcement_enabled", "0") != "1" or await db.get_setting("enforcement_backstop", "1") != "1":
        return
    sid = session.playback_session_id
    if not sid or sid in _backstopped or not session.client_identifier:
        return
    if await db.is_proxied(session.client_identifier, session.rating_key):
        return
    from .stream_proxy import eligible_ranges

    resolve_fn = getattr(db, "resolve_session_version", None)
    vid = (
        await resolve_fn(
            rating_key=session.rating_key,
            plex_guid=session.plex_guid,
            part_id=getattr(session, "part_id", 0),
            plex_media_id=getattr(session, "plex_media_id", ""),
            file_path=getattr(session, "file_path", ""),
        )
        if resolve_fn is not None
        else None
    )
    ranges = eligible_ranges()
    eligible = await (ranges(session.rating_key, session.user, vid) if vid else ranges(session.rating_key, session.user))
    if not eligible:
        return
    first = _first_seen.setdefault(sid, time.monotonic())
    if time.monotonic() - first < BACKSTOP_GRACE_S:
        return
    _backstopped.add(sid)
    stopped = await client.terminate_session(sid, "This title is filtered. Play it again to watch the filtered version.")
    logger.warning("Backstop %s %s's stream of '%s' on %s: it did not pass the stream proxy",
                   "stopped" if stopped else "could not stop", session.user, session.full_title, session.client_title)


# client_identifier -> last recorded value, so the DB is written only when it changes.
_recorded_control: dict[str, bool] = {}


async def _remember_client_control(session) -> None:
    """Record a client live skipping cannot reach (not a local player), for the stream proxy.

    Only the negative is recorded here. Whether commands really get through is learned from
    their outcome (PlexClient._record_reachability, #184): a reported player state proves nothing.
    """
    if not session.client_identifier:
        return
    if not (session.is_controllable and session.client_local) and _recorded_control.get(session.client_identifier) is not False:
        await db.record_client_control(session.client_identifier, False)
        _recorded_control[session.client_identifier] = False


async def _next_poll_delay(sessions, config) -> float:
    """Return how long to sleep before the next session poll.

    Polling tightens as a session approaches a segment and relaxes when every
    session is far from one. A fixed interval forces a trade between Plex API load
    and skip precision; this gives precision only where it is needed.
    """
    delay = float(config.poll_interval)
    if not sessions:
        return delay

    for session in sessions:
        try:
            sess_fn = getattr(db, "get_segments_for_session", None)
            if sess_fn is not None and (
                getattr(session, "part_id", 0) or getattr(session, "plex_media_id", "") or getattr(session, "file_path", "")
            ):
                segments = await sess_fn(
                    rating_key=session.rating_key,
                    plex_guid=session.plex_guid,
                    part_id=getattr(session, "part_id", 0),
                    plex_media_id=getattr(session, "plex_media_id", ""),
                    file_path=getattr(session, "file_path", ""),
                )
            else:
                segments = await db.get_segments_for_guid(session.plex_guid)
                if not segments and session.rating_key:
                    segments = await db.get_segments_by_rating_key(session.rating_key)
            upcoming = [
                s["start_ms"] - config.pre_buffer_ms - session.position_ms
                for s in segments
                if s["start_ms"] - config.pre_buffer_ms > session.position_ms
            ]
            if upcoming:
                delay = min(delay, max(MIN_POLL_INTERVAL_S, min(upcoming) / 1000.0))
        except Exception as exc:
            logger.debug("Could not compute poll delay for %s: %s", session.session_key, exc)

    return max(MIN_POLL_INTERVAL_S, delay)


async def _import_sidecar(item, version_id: int | None = None, file_path: str | None = None) -> bool:
    """Import a skip file sitting next to the media, if there is one.

    Returns True when segments were imported, meaning the version needs no ML scan.
    File I/O is small (a few KB of text) and runs once per newly discovered version,
    so it is read inline rather than through a thread.
    """
    try:
        sidecar = importers.find_sidecar(file_path or item.file_path)
        if sidecar is None:
            return False

        segments, source = importers.parse_file(sidecar)
        count = await db.insert_segments_bulk(
            item.plex_guid, item.title, segments, source, version_id=version_id
        )
        events.publish("segments_changed", guid=item.plex_guid)
        await db.update_scan_job_status(item.plex_guid, "completed", progress=100, version_id=version_id)
        logger.info(
            "Imported %d segment(s) for '%s' from %s — skipping ML scan",
            count, item.title, sidecar.name,
        )
        return count > 0
    except Exception as exc:
        # A malformed sidecar must not stop the title being scanned normally.
        logger.warning("Could not import sidecar for '%s': %s", item.title, exc)
        return False


def _item_versions_payload(item) -> list[dict]:
    """Build version dicts from a Plex MediaItem (supporting multi-version items and simple fakes)."""
    versions = getattr(item, "versions", None)
    if versions:
        return [
            {
                "plex_media_id": str(v.plex_media_id),
                "media_index": idx,
                "resolution": v.resolution or "",
                "file_path": v.file_path,
                "part_files": json.dumps(v.part_files or [v.file_path]),
                "part_ids": json.dumps(v.part_ids or []),
                "file_size": int(v.file_size or 0),
                "duration_ms": int(v.duration_ms or 0),
            }
            for idx, v in enumerate(versions)
            if v.file_path
        ]
    return [
        {
            "plex_media_id": "",
            "media_index": 0,
            "resolution": "",
            "file_path": item.file_path,
            "part_files": json.dumps(getattr(item, "part_files", None) or [item.file_path]),
            "part_ids": json.dumps(getattr(item, "part_ids", None) or []),
            "file_size": 0,
            "duration_ms": int(getattr(item, "duration_ms", 0) or 0),
        }
    ]


async def library_watcher_loop(get_config_fn, get_client_fn) -> None:
    """Periodically check for new Plex library items/versions and enqueue unscanned ones."""
    first_run = True
    while True:
        if not first_run:
            await asyncio.sleep(LIBRARY_POLL_INTERVAL_S)
        first_run = False

        config = await get_config_fn()
        if not config.is_configured():
            continue

        try:
            client = get_client_fn()
            sections = await client.get_library_sections()
            excluded = set(json.loads(await db.get_setting("excluded_library_ids", "[]")))
            scan_ratings = set(json.loads(await db.get_setting("scan_ratings", "[]")))

            for section in sections:
                if section.section_id in excluded:
                    continue
                items = await client.get_library_items(section.section_id)
                candidates = [
                    i for i in items
                    if i.file_path and rating_allowed(i.content_rating, scan_ratings)
                ]
                # One chunked query for the whole section instead of one per item (#162).
                known = await db.get_scan_jobs_by_guids([i.plex_guid for i in candidates])
                fresh = [i for i in candidates if i.plex_guid not in known]
                await db.upsert_scan_jobs_batch([
                    {
                        "plex_guid": i.plex_guid, "title": i.title, "file_path": i.file_path,
                        "rating_key": i.rating_key, "library_id": i.library_id,
                        "library_title": i.library_title, "content_rating": i.content_rating,
                        "media_type": i.media_type, "year": i.year, "show_guid": i.show_guid,
                        "part_files": json.dumps(i.part_files), "duration_ms": i.duration_ms,
                        "versions": _item_versions_payload(i),
                    }
                    for i in fresh
                ])
                existing_items = [i for i in candidates if i.plex_guid in known]
                sync_res = await db.sync_title_versions_batch([
                    {
                        "plex_guid": i.plex_guid,
                        "rating_key": i.rating_key,
                        "title": i.title,
                        "versions": _item_versions_payload(i),
                    }
                    for i in existing_items
                ])
                # Map direct-play part ids -> (rating_key, version_id) across every version (#184, #233).
                versions_map = await db.get_title_versions_for_guids([i.plex_guid for i in items if i.file_path])
                part_pairs: list[tuple[int, str, int]] = []
                for it in items:
                    if not it.file_path:
                        continue
                    vlist = versions_map.get(it.plex_guid, [])
                    by_mid = {v["plex_media_id"]: v["id"] for v in vlist if v["plex_media_id"]}
                    by_path = {v["file_path"]: v["id"] for v in vlist if v["file_path"]}
                    fallback_vid = vlist[0]["id"] if vlist else 0
                    for ver in getattr(it, "versions", []) or []:
                        vid = by_mid.get(str(ver.plex_media_id)) or by_path.get(ver.file_path) or fallback_vid
                        for pid in ver.part_ids:
                            part_pairs.append((pid, it.rating_key, vid))
                    if not getattr(it, "versions", None):
                        for pid in it.part_ids:
                            part_pairs.append((pid, it.rating_key, fallback_vid))
                await db.replace_title_parts(part_pairs)

                fresh_by_guid = {i.plex_guid: i for i in fresh}
                existing_by_guid = {i.plex_guid: i for i in existing_items}
                to_queue_vids: list[int] = []
                to_queue_guids: list[str] = []

                for item in fresh:
                    vlist = versions_map.get(item.plex_guid, [])
                    if vlist:
                        for v in vlist:
                            if await _import_sidecar(item, version_id=v["id"], file_path=v["file_path"]):
                                continue
                            to_queue_vids.append(v["id"])
                            logger.info("New version queued for scan: %s (%s)", item.title, v.get("resolution") or v["file_path"])
                    else:
                        if await _import_sidecar(item):
                            continue
                        to_queue_guids.append(item.plex_guid)
                        logger.info("New item queued for scan: %s", item.title)

                for nv in sync_res.get("inserted", []):
                    parent = existing_by_guid.get(nv["plex_guid"]) or fresh_by_guid.get(nv["plex_guid"])
                    if parent is None:
                        continue
                    if await _import_sidecar(parent, version_id=nv["id"], file_path=nv["file_path"]):
                        continue
                    to_queue_vids.append(nv["id"])
                    logger.info("Added version queued for scan: %s (%s)", parent.title, nv.get("resolution") or nv["file_path"])

                if to_queue_vids:
                    await enqueue_versions(to_queue_vids)
                if to_queue_guids:
                    await enqueue_many(to_queue_guids)

        except Exception as exc:
            logger.warning("Library watcher error: %s", exc)
