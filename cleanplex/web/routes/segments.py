import asyncio
import hashlib
import json
import mimetypes
import os
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from ...logger import get_logger
import cleanplex.plex_client as plex_mod
from ... import database as db

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["segments"])

# Pending debounce tasks keyed by plex_guid — cancelled and replaced on each delete.
# This ensures bulk deletes trigger at most one Plex metadata write per title.
_pending_summary_tasks: dict[str, asyncio.Task] = {}

# scan_labels changes only when the user edits Settings, so a 30-second TTL avoids
# opening a second aiosqlite connection on every segment-panel expand.
_scan_labels_cache: tuple[float, str] | None = None  # (monotonic_time, raw_json_str)
_SCAN_LABELS_CACHE_TTL = 30.0

# Plex artwork proxy cache — keyed by image path, value is (monotonic_time, bytes, content_type).
# Artwork rarely changes; 1-hour TTL avoids repeated outbound Plex HTTP calls.
# Capped at 300 entries to bound memory; oldest entry is evicted on overflow.
_plex_image_cache: dict[str, tuple[float, bytes, str]] = {}
_PLEX_IMAGE_CACHE_TTL = 3600.0
_PLEX_IMAGE_CACHE_MAX = 300

# Disk-based poster cache: persists across server restarts and survives process memory limits.
# Files are stored under ~/.cleanplex/posters/ and refreshed when older than 7 days.
_POSTERS_DIR = Path.home() / ".cleanplex" / "posters"
_POSTER_DISK_TTL = 7 * 24 * 3600.0  # 7 days


def _invalidate_scan_labels_cache() -> None:
    global _scan_labels_cache
    _scan_labels_cache = None


async def _do_refresh_summary(plex_guid: str) -> None:
    """Perform the actual Plex summary metadata update after a short debounce delay."""
    await asyncio.sleep(2)  # coalesce rapid sequential deletes
    _pending_summary_tasks.pop(plex_guid, None)

    try:
        job = await db.get_scan_job_by_guid(plex_guid)
        if not job:
            return

        rating_key = str(job.get("rating_key") or "")
        if not rating_key:
            return

        # COUNT query — avoids loading all segment rows just to get the total.
        segment_count = await db.count_segments_for_guid(plex_guid)
        status = "Scanned" if job.get("status") == "done" else "Pending"

        client = plex_mod.get_client()
        await client.update_cleanplex_summary(
            rating_key=rating_key,
            status=status,
            segment_count=segment_count,
        )
    except Exception as exc:
        logger.debug("Could not refresh Plex summary for guid=%s: %s", plex_guid, exc)


def _refresh_cleanplex_summary_for_guid(plex_guid: str) -> None:
    """Schedule a debounced Plex summary refresh.

    Any pending refresh for the same guid is cancelled before scheduling a new
    one, so bulk deletes of N segments produce at most one metadata write.
    """
    existing = _pending_summary_tasks.pop(plex_guid, None)
    if existing and not existing.done():
        existing.cancel()
    task = asyncio.create_task(_do_refresh_summary(plex_guid))
    _pending_summary_tasks[plex_guid] = task


def _plex_image_proxy_url(path: str) -> str:
    if not path:
        return ""
    return f"/api/plex-image?path={quote(path, safe='')}"


# ── Libraries / titles tree ───────────────────────────────────────────────────

@router.get("/libraries")
async def get_libraries():
    """Return all Plex library sections."""
    try:
        client = plex_mod.get_client()
        sections = await client.get_library_sections()
        return {
            "libraries": [
                {"id": s.section_id, "title": s.title, "type": s.section_type}
                for s in sections
            ]
        }
    except RuntimeError:
        return {"libraries": [], "error": "Plex not configured"}


@router.post("/libraries/{library_id}/sync")
async def sync_library(library_id: str):
    """Sync a library from Plex into DB. Returns count of new titles added."""
    excluded = set(json.loads(await db.get_setting("excluded_library_ids", "[]")))
    if library_id in excluded:
        return {"ok": False, "error": "Library is excluded from scanning"}
    try:
        client = plex_mod.get_client()
        items = await client.get_library_items(library_id)
        logger.info(f"Syncing library {library_id}: found {len(items)} items from Plex")

        # Sync all titles regardless of scan_ratings — the scanner applies that
        # filter when deciding what to scan, not at discovery time.
        file_items = [i for i in items if i.file_path]
        existing_guids = await db.get_existing_guids([i.plex_guid for i in file_items])

        # Refresh mutable Plex metadata for all existing titles in one transaction
        # so that manual rating changes in Plex are reflected after sync.
        await db.refresh_scan_job_metadata_batch([
            (i.plex_guid, i.title, i.file_path, i.rating_key, i.content_rating, i.year, i.show_guid, i.show_rating_key, json.dumps(i.part_files))
            for i in file_items if i.plex_guid in existing_guids
        ])

        # Remove DB entries for titles Plex no longer reports in this library
        # (e.g. deleted files or removed duplicates).
        plex_guids = [i.plex_guid for i in file_items]
        removed = await db.delete_scan_jobs_not_in(library_id, plex_guids)
        if removed:
            logger.info(f"Library {library_id} sync: removed {removed} stale titles")

        added = 0
        for item in file_items:
            if item.plex_guid in existing_guids:
                continue
            await db.upsert_scan_job(
                plex_guid=item.plex_guid,
                title=item.title,
                file_path=item.file_path,
                rating_key=item.rating_key,
                library_id=item.library_id,
                library_title=item.library_title,
                content_rating=item.content_rating,
                media_type=item.media_type,
                year=item.year,
                show_guid=item.show_guid,
                show_rating_key=item.show_rating_key,
                part_files=json.dumps(item.part_files),
            )
            added += 1

        logger.info(f"Library {library_id} synced: {added} new titles added")
        return {"ok": True, "synced": len(items), "new": added, "removed": removed}
    except RuntimeError as e:
        logger.error(f"Plex client error during library sync: {e}")
        return {"ok": False, "error": str(e)}


@router.get("/libraries/{library_id}/titles")
async def get_titles_in_library(library_id: str):
    """Return all scan jobs (titles) for a given library, with Plex poster URLs.

    Poster URLs are built from show_rating_key stored in the DB — no Plex API call
    is needed per episode. Falls back to get_episode_show_art for rows that predate
    the show_rating_key column (e.g. episodes synced before this change).
    """
    jobs = await db.get_scan_jobs_by_library(library_id)
    seg_counts = await db.get_segment_counts_for_library(library_id)

    try:
        client = plex_mod.get_client()
    except RuntimeError:
        client = None

    result = []
    # Fallback cache for episodes whose show_rating_key is not yet in the DB.
    # Keyed by parsed show name; value: (show_guid, show_title, poster_url, show_rating_key, season_rating_key)
    show_meta_fallback: dict[str, tuple[str, str, str, str, str]] = {}

    for job in jobs:
        thumb_url = ""
        poster_url = ""
        show_guid = job.get("show_guid") or ""
        show_title = ""
        show_rating_key = job.get("show_rating_key") or ""
        season_rating_key = ""

        if client and job.get("rating_key"):
            rating_key = job["rating_key"]
            thumb_url = _plex_image_proxy_url(f"/library/metadata/{rating_key}/thumb")

            if job.get("media_type") == "episode":
                parsed_show_name = (job.get("title", "").split("\u2013")[0].strip())

                if show_rating_key:
                    # Fast path: build poster URL directly from DB data — zero Plex API calls.
                    poster_url = _plex_image_proxy_url(f"/library/metadata/{show_rating_key}/thumb")
                    show_title = parsed_show_name
                else:
                    # Slow fallback: Plex API call for old rows missing show_rating_key.
                    # Once these rows are re-synced from Plex the fast path takes over.
                    if parsed_show_name and parsed_show_name in show_meta_fallback:
                        show_guid, show_title, poster_url, show_rating_key, season_rating_key = show_meta_fallback[parsed_show_name]
                    else:
                        resolved = await client.get_episode_show_art(rating_key)
                        show_guid = resolved[0] or show_guid
                        show_title = resolved[1] or parsed_show_name
                        poster_url = _plex_image_proxy_url(resolved[2]) if resolved[2] else ""
                        show_rating_key = resolved[3]
                        season_rating_key = resolved[4]
                        if parsed_show_name and (show_guid or poster_url):
                            show_meta_fallback[parsed_show_name] = (show_guid, show_title, poster_url, show_rating_key, season_rating_key)
            else:
                poster_url = thumb_url

        result.append({
            "plex_guid": job["plex_guid"],
            "rating_key": job.get("rating_key", ""),
            "title": job["title"],
            "status": job["status"],
            "progress": job["progress"],
            "finished_at": job.get("finished_at"),
            "thumb_url": thumb_url,
            "poster_url": poster_url,
            "show_guid": show_guid,
            "show_title": show_title,
            "show_rating_key": show_rating_key,
            "season_rating_key": season_rating_key,
            "segment_count": seg_counts.get(job["plex_guid"], 0),
            "content_rating": job.get("content_rating", ""),
            "media_type": job.get("media_type", "movie"),
            "year": job.get("year"),
            "ignored": bool(job.get("ignored", 0)),
        })
    return {"titles": result}


def _poster_disk_path(image_path: str) -> Path:
    """Return the disk cache path for a Plex image path, keyed by MD5 of the path string."""
    key = hashlib.md5(image_path.encode()).hexdigest()
    return _POSTERS_DIR / key


@router.get("/plex-image")
async def get_plex_image(path: str):
    """Proxy Plex images through Cleanplex so artwork loads from remote clients.

    Cache hierarchy:
    1. In-memory cache (1-hour TTL) — fastest, avoids disk reads on repeated requests.
    2. Disk cache under ~/.cleanplex/posters/ (7-day TTL) — survives server restarts.
    3. Plex API fetch — writes to both disk and memory caches on miss.
    """
    if not path:
        raise HTTPException(status_code=400, detail="Missing image path")

    now = time.monotonic()

    # 1. In-memory cache hit
    cached = _plex_image_cache.get(path)
    if cached and now - cached[0] < _PLEX_IMAGE_CACHE_TTL:
        return Response(
            content=cached[1],
            media_type=cached[2] or "image/jpeg",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    # 2. Disk cache hit — file exists and is younger than 7 days
    disk_path = _poster_disk_path(path)
    if disk_path.exists():
        age = time.time() - disk_path.stat().st_mtime
        if age < _POSTER_DISK_TTL:
            content = disk_path.read_bytes()
            content_type = "image/jpeg"
            # Warm the in-memory cache from disk so subsequent requests skip disk I/O.
            if len(_plex_image_cache) >= _PLEX_IMAGE_CACHE_MAX:
                oldest = min(_plex_image_cache, key=lambda k: _plex_image_cache[k][0])
                del _plex_image_cache[oldest]
            _plex_image_cache[path] = (now, content, content_type)
            return Response(
                content=content,
                media_type=content_type,
                headers={"Cache-Control": "public, max-age=3600"},
            )

    # 3. Fetch from Plex — cache result to both disk and memory
    try:
        client = plex_mod.get_client()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Plex not configured")

    content, content_type = await client.fetch_image(path)
    if not content:
        raise HTTPException(status_code=404, detail="Image not available")

    # Write to disk (best-effort — never fail the request on write errors).
    try:
        _POSTERS_DIR.mkdir(parents=True, exist_ok=True)
        disk_path.write_bytes(content)
    except Exception as exc:
        logger.debug("Failed to write poster cache to disk for %s: %s", path, exc)

    # Evict oldest entry when memory cache is full.
    if len(_plex_image_cache) >= _PLEX_IMAGE_CACHE_MAX:
        oldest = min(_plex_image_cache, key=lambda k: _plex_image_cache[k][0])
        del _plex_image_cache[oldest]
    _plex_image_cache[path] = (now, content, content_type)

    return Response(
        content=content,
        media_type=content_type or "image/jpeg",
        # Plex artwork is stable; let browsers cache for 1 hour before revalidating.
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.post("/titles/segments/batch")
async def get_segments_batch(data: dict):
    """Return segments for a list of plex_guids in a single DB query.

    Accepts {"guids": ["guid1", "guid2", ...]} and applies the same
    label-filtering logic as the single-title endpoint.
    """
    guids: list[str] = data.get("guids", [])
    if not guids:
        return {"segments": []}

    global _scan_labels_cache
    now = time.monotonic()
    if _scan_labels_cache and now - _scan_labels_cache[0] < _SCAN_LABELS_CACHE_TTL:
        segments = await db.get_segments_for_guids(guids)
        scan_labels_raw_str = _scan_labels_cache[1]
    else:
        # Fetch segments + setting in one connection.
        async with db.get_connection() as conn:
            placeholders = ",".join("?" * len(guids))
            rows = await conn.execute_fetchall(
                f"SELECT * FROM segments WHERE plex_guid IN ({placeholders}) ORDER BY plex_guid, start_ms",
                guids,
            )
            segments = [dict(r) for r in rows]
            row = await (await conn.execute("SELECT value FROM settings WHERE key=?", ("scan_labels",))).fetchone()
            scan_labels_raw_str = row["value"] if row else "[]"
        _scan_labels_cache = (now, scan_labels_raw_str)

    scan_labels_raw = json.loads(scan_labels_raw_str)
    enabled_labels = set(scan_labels_raw) if isinstance(scan_labels_raw, list) else set()
    result = []
    for seg in segments:
        labels = seg.get("labels", "") or ""
        if enabled_labels and labels:
            filtered = [l.strip() for l in labels.split(",") if l.strip() in enabled_labels]
            labels = ",".join(filtered)
        result.append({
            "id": seg["id"],
            "plex_guid": seg["plex_guid"],
            "title": seg["title"],
            "start_ms": seg["start_ms"],
            "end_ms": seg["end_ms"],
            "confidence": seg["confidence"],
            "has_thumbnail": bool(seg.get("thumbnail_path")),
            "thumbnail_url": f"/api/thumbnails/{seg['id']}" if seg.get("thumbnail_path") else "",
            "created_at": seg["created_at"],
            "labels": labels,
        })
    return Response(
        content=json.dumps({"segments": result}),
        media_type="application/json",
        # Short browser cache so toggling the same panel within 30 s skips the round-trip.
        headers={"Cache-Control": "private, max-age=30"},
    )


@router.get("/titles/{plex_guid:path}/segments")
async def get_segments_for_title(plex_guid: str):
    """Return all segments for a specific title with all detected labels."""
    global _scan_labels_cache
    now = time.monotonic()
    if _scan_labels_cache and now - _scan_labels_cache[0] < _SCAN_LABELS_CACHE_TTL:
        # Cache hit: fetch segments only — single DB connection open.
        segments = await db.get_segments_for_guid(plex_guid)
        scan_labels_raw_str = _scan_labels_cache[1]
    else:
        # Cache miss: fetch segments + setting together in one connection open.
        segments, scan_labels_raw_str = await db.get_segments_for_guid_with_setting(
            plex_guid, "scan_labels", "[]"
        )
        _scan_labels_cache = (now, scan_labels_raw_str)
    scan_labels_raw = json.loads(scan_labels_raw_str)
    enabled_labels = set(scan_labels_raw) if isinstance(scan_labels_raw, list) else set()
    result = []
    for seg in segments:
        labels = seg.get("labels", "") or ""
        if enabled_labels and labels:
            filtered = [l.strip() for l in labels.split(",") if l.strip() in enabled_labels]
            labels = ",".join(filtered)
        result.append({
            "id": seg["id"],
            "plex_guid": seg["plex_guid"],
            "title": seg["title"],
            "start_ms": seg["start_ms"],
            "end_ms": seg["end_ms"],
            "confidence": seg["confidence"],
            "has_thumbnail": bool(seg.get("thumbnail_path")),
            "thumbnail_url": f"/api/thumbnails/{seg['id']}" if seg.get("thumbnail_path") else "",
            "created_at": seg["created_at"],
            "labels": labels,
        })
    return Response(
        content=json.dumps({"segments": result}),
        media_type="application/json",
        # Short browser cache so toggling the same panel within 30 s skips the round-trip.
        headers={"Cache-Control": "private, max-age=30"},
    )


@router.post("/segments/{segment_id}/jump")
async def jump_to_segment(segment_id: int):
    """Seek an active, controllable Plex session for this title to the segment start."""
    seg = await db.get_segment_by_id(segment_id)
    if not seg:
        raise HTTPException(status_code=404, detail="Segment not found")

    job = await db.get_scan_job_by_guid(seg["plex_guid"])

    try:
        client = plex_mod.get_client()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Plex not configured")

    sessions = await client.get_active_sessions()

    target = None
    rating_key = str(job.get("rating_key", "")) if job else ""

    if rating_key:
        target = next(
            (s for s in sessions if s.is_controllable and str(s.rating_key) == rating_key),
            None,
        )

    if target is None:
        target = next(
            (s for s in sessions if s.is_controllable and s.plex_guid == seg["plex_guid"]),
            None,
        )

    if target is None:
        raise HTTPException(
            status_code=409,
            detail="No active controllable Plex playback found for this title. Start the title in Plex first.",
        )

    ok = await client.seek(
        target.client_identifier,
        int(seg["start_ms"]),
        target.client_address,
        target.client_port,
    )
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to seek Plex client")

    return {
        "ok": True,
        "segment_id": segment_id,
        "seek_to_ms": int(seg["start_ms"]),
        "client": target.client_title,
        "user": target.user,
    }


@router.get("/segments/{segment_id}/stream")
async def stream_segment_source(segment_id: int):
    """Stream the source media file for a segment so the web UI can preview it directly."""
    seg = await db.get_segment_by_id(segment_id)
    if not seg:
        raise HTTPException(status_code=404, detail="Segment not found")

    job = await db.get_scan_job_by_guid(seg["plex_guid"])
    if not job or not job.get("file_path"):
        raise HTTPException(status_code=404, detail="Source file not found for this segment")

    file_path = job["file_path"]
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="Source media file does not exist on disk")

    media_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    return FileResponse(path=file_path, media_type=media_type)


@router.delete("/segments/{segment_id}")
async def delete_segment(segment_id: int):
    seg = await db.get_segment_by_id(segment_id)
    if not seg:
        raise HTTPException(status_code=404, detail="Segment not found")

    deleted = await db.delete_segment(segment_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Segment not found")

    _refresh_cleanplex_summary_for_guid(seg["plex_guid"])

    return {"ok": True}


@router.delete("/titles/{plex_guid:path}/segments")
async def delete_all_segments_for_title(plex_guid: str):
    """Delete all segments for a specific title."""
    deleted = await db.delete_segments_for_guid(plex_guid)
    _refresh_cleanplex_summary_for_guid(plex_guid)
    return {"ok": True, "deleted": deleted}


@router.get("/segments")
async def get_all_segments(limit: int = 100, offset: int = 0):
    segments = await db.get_all_segments(limit=limit, offset=offset)
    return {"segments": segments}
