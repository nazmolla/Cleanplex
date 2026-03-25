"""SQLite database access layer using aiosqlite."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from .logger import get_logger

logger = get_logger(__name__)

_DB_PATH: Path | None = None


def set_db_path(path: Path) -> None:
    global _DB_PATH
    _DB_PATH = path


def get_db_path() -> Path:
    if _DB_PATH is None:
        raise RuntimeError("Database path not configured. Call set_db_path() first.")
    return _DB_PATH


@asynccontextmanager
async def get_connection():
    async with aiosqlite.connect(str(get_db_path())) as conn:
        conn.row_factory = aiosqlite.Row
        # foreign_keys is connection-scoped and must be set per connection.
        # journal_mode=WAL is persistent after init_db(); no need to repeat it here.
        await conn.execute("PRAGMA foreign_keys=ON")
        yield conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    plex_guid     TEXT    NOT NULL,
    title         TEXT,
    start_ms      INTEGER NOT NULL,
    end_ms        INTEGER NOT NULL,
    confidence    REAL    DEFAULT 0,
    labels        TEXT    DEFAULT '',
    thumbnail_path TEXT,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_segments_guid ON segments(plex_guid);

CREATE TABLE IF NOT EXISTS user_filters (
    plex_username TEXT PRIMARY KEY,
    enabled       INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS scan_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plex_guid   TEXT    UNIQUE NOT NULL,
    title       TEXT,
    file_path   TEXT,
    rating_key  TEXT,
    library_id  TEXT,
    library_title TEXT,
    media_type  TEXT DEFAULT 'movie',
    content_rating TEXT DEFAULT '',
    year        INTEGER,
    status      TEXT DEFAULT 'pending',
    progress    REAL DEFAULT 0,
    force_scan  INTEGER DEFAULT 0,
    ignored     INTEGER DEFAULT 0,
    started_at  TIMESTAMP,
    finished_at TIMESTAMP,
    error_msg   TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS segment_library_entries (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash         TEXT    NOT NULL,
    file_name         TEXT    NOT NULL,
    file_size         INTEGER,
    duration_ms       INTEGER,
    segments_json     TEXT    NOT NULL,
    cloud_version     INTEGER DEFAULT 0,
    source_instance   TEXT    NOT NULL,
    confidence_level  TEXT    DEFAULT 'local',
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(file_hash, source_instance)
);
CREATE INDEX IF NOT EXISTS idx_segment_library_hash ON segment_library_entries(file_hash);

CREATE TABLE IF NOT EXISTS sync_metadata (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_name             TEXT    NOT NULL UNIQUE,
    github_repo               TEXT,
    github_token              TEXT,
    last_sync_time            TIMESTAMP,
    sync_enabled              INTEGER DEFAULT 0,
    conflict_resolution       TEXT    DEFAULT 'consensus',
    verified_threshold        INTEGER DEFAULT 2,
    timing_tolerance_ms       INTEGER DEFAULT 2000,
    created_at                TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bg_jobs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type           TEXT    NOT NULL,
    status             TEXT    DEFAULT 'queued',
    progress_percent   INTEGER DEFAULT 0,
    result_data        TEXT,
    error_message      TEXT,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at         TIMESTAMP,
    completed_at       TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_bg_jobs_status ON bg_jobs(status, created_at DESC);
"""

DEFAULT_SETTINGS = {
    "plex_url": "",
    "plex_token": "",
    "poll_interval": "5",
    "confidence_threshold": "0.6",
    "skip_buffer_ms": "3000",
    "scan_step_ms": "5000",
    "scan_workers": "2",
    "segment_gap_ms": "12000",
    "segment_min_hits": "1",
    "scan_window_start": "23:00",
    "scan_window_end": "06:00",
    "log_level": "INFO",
    "excluded_library_ids": "[]",
    "scan_ratings": "[]",  # empty = scan all ratings
    "scan_labels": "[\"FEMALE_BREAST_EXPOSED\",\"FEMALE_GENITALIA_EXPOSED\",\"MALE_GENITALIA_EXPOSED\",\"ANUS_EXPOSED\",\"BUTTOCKS_EXPOSED\"]",
    "nudenet_model": "320n",
    "nudenet_model_path": "",
    # Segment library sharing settings
    "sync_enabled": "0",
    "sync_instance_name": "",
    "sync_github_repo": "",
    "sync_conflict_resolution": "consensus",
    "sync_verified_threshold": "2",
    "sync_timing_tolerance_ms": "2000",
}


async def init_db() -> None:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with get_connection() as conn:
        # WAL mode is a persistent DB property; set it once at init rather than
        # on every connection to avoid redundant per-connection overhead.
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.executescript(SCHEMA)
        # Additive schema migrations — each ALTER TABLE is idempotent.
        # Catch only the specific OperationalError SQLite raises for duplicate columns;
        # any other error (e.g. corrupt DB, permission failure) is re-raised immediately.
        migrations = [
            "ALTER TABLE scan_jobs ADD COLUMN content_rating TEXT DEFAULT ''",
            "ALTER TABLE scan_jobs ADD COLUMN media_type TEXT DEFAULT 'movie'",
            "ALTER TABLE scan_jobs ADD COLUMN force_scan INTEGER DEFAULT 0",
            "ALTER TABLE scan_jobs ADD COLUMN year INTEGER",
            "ALTER TABLE segments ADD COLUMN labels TEXT DEFAULT ''",
            "ALTER TABLE scan_jobs ADD COLUMN ignored INTEGER DEFAULT 0",
            # show_guid stores the Plex grandparentGuid for episodes so the
            # scan queue can group all episodes of a show together.
            "ALTER TABLE scan_jobs ADD COLUMN show_guid TEXT DEFAULT ''",
        ]
        for stmt in migrations:
            try:
                await conn.execute(stmt)
                await conn.commit()
            except aiosqlite.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    # Unexpected error — surface it rather than silently continuing.
                    logger.error("Unexpected migration failure: %s — %s", stmt, exc)
                    raise
        for key, value in DEFAULT_SETTINGS.items():
            await conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )
        await conn.commit()
    logger.info("Database initialised at %s", db_path)


# ── Settings ──────────────────────────────────────────────────────────────────

async def get_all_settings() -> dict[str, str]:
    async with get_connection() as conn:
        rows = await conn.execute_fetchall("SELECT key, value FROM settings")
        return {row["key"]: row["value"] for row in rows}


async def get_setting(key: str, default: str = "") -> str:
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT value FROM settings WHERE key=?", (key,))).fetchone()
        return row["value"] if row else default


async def set_setting(key: str, value: str) -> None:
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await conn.commit()


async def update_settings(data: dict[str, str]) -> None:
    async with get_connection() as conn:
        for key, value in data.items():
            await conn.execute(
                "INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        await conn.commit()


# ── User Filters ──────────────────────────────────────────────────────────────

async def get_all_user_filters() -> list[dict]:
    async with get_connection() as conn:
        rows = await conn.execute_fetchall("SELECT plex_username, enabled FROM user_filters ORDER BY plex_username")
        return [dict(r) for r in rows]


async def get_user_filter(username: str) -> dict | None:
    async with get_connection() as conn:
        row = await (await conn.execute(
            "SELECT plex_username, enabled FROM user_filters WHERE plex_username=?", (username,)
        )).fetchone()
        return dict(row) if row else None


async def upsert_user_filter(username: str, enabled: bool) -> None:
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO user_filters(plex_username, enabled) VALUES(?,?) "
            "ON CONFLICT(plex_username) DO UPDATE SET enabled=excluded.enabled",
            (username, 1 if enabled else 0),
        )
        await conn.commit()


# ── Segments ──────────────────────────────────────────────────────────────────

async def get_segments_for_guid(plex_guid: str) -> list[dict]:
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM segments WHERE plex_guid=? ORDER BY start_ms", (plex_guid,)
        )
        return [dict(r) for r in rows]


async def get_segments_by_rating_key(rating_key: str) -> list[dict]:
    """Look up segments by scan_jobs.rating_key — fallback when session GUID differs from stored GUID."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """SELECT s.* FROM segments s
               JOIN scan_jobs j ON j.plex_guid = s.plex_guid
               WHERE j.rating_key = ?
               ORDER BY s.start_ms""",
            (rating_key,),
        )
        return [dict(r) for r in rows]


async def count_segments_for_guid(plex_guid: str) -> int:
    """Return segment count for a title using SELECT COUNT — avoids loading full rows."""
    async with get_connection() as conn:
        row = await (
            await conn.execute("SELECT COUNT(*) FROM segments WHERE plex_guid=?", (plex_guid,))
        ).fetchone()
        return row[0] if row else 0


async def delete_segments_for_guid(plex_guid: str) -> int:
    """Delete all stored segments for a title and return deleted row count."""
    async with get_connection() as conn:
        cursor = await conn.execute("DELETE FROM segments WHERE plex_guid=?", (plex_guid,))
        await conn.commit()
        return cursor.rowcount


async def get_all_segments(limit: int = 200, offset: int = 0) -> list[dict]:
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM segments ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [dict(r) for r in rows]


async def insert_segment(
    plex_guid: str,
    title: str,
    start_ms: int,
    end_ms: int,
    confidence: float = 0.0,
    thumbnail_path: str | None = None,
    labels: str = "",
) -> int:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO segments(plex_guid, title, start_ms, end_ms, confidence, thumbnail_path, labels) "
            "VALUES(?,?,?,?,?,?,?)",
            (plex_guid, title, start_ms, end_ms, confidence, thumbnail_path, labels),
        )
        await conn.commit()
        return cursor.lastrowid


async def delete_segment(segment_id: int) -> bool:
    async with get_connection() as conn:
        cursor = await conn.execute("DELETE FROM segments WHERE id=?", (segment_id,))
        await conn.commit()
        return cursor.rowcount > 0


async def get_segment_by_id(segment_id: int) -> dict | None:
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT * FROM segments WHERE id=?", (segment_id,))).fetchone()
        return dict(row) if row else None


async def get_segments_grouped_by_title() -> list[dict]:
    """Return distinct (plex_guid, title) pairs that have segments."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT plex_guid, title, COUNT(*) as segment_count FROM segments GROUP BY plex_guid ORDER BY title"
        )
        return [dict(r) for r in rows]


# ── Scan Jobs ─────────────────────────────────────────────────────────────────

async def upsert_scan_job(
    plex_guid: str,
    title: str,
    file_path: str,
    rating_key: str,
    library_id: str,
    library_title: str,
    content_rating: str = "",
    media_type: str = "movie",
    year: int | None = None,
    show_guid: str = "",
) -> None:
    async with get_connection() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO scan_jobs"
            "(plex_guid, title, file_path, rating_key, library_id, library_title, content_rating, media_type, year, show_guid) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (plex_guid, title, file_path, rating_key, library_id, library_title, content_rating, media_type, year, show_guid),
        )
        await conn.commit()


async def get_scan_jobs(status: str | None = None) -> list[dict]:
    async with get_connection() as conn:
        if status:
            rows = await conn.execute_fetchall(
                "SELECT * FROM scan_jobs WHERE status=? ORDER BY created_at DESC", (status,)
            )
        else:
            rows = await conn.execute_fetchall("SELECT * FROM scan_jobs ORDER BY created_at DESC")
        return [dict(r) for r in rows]


async def get_scan_job_by_guid(plex_guid: str) -> dict | None:
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT * FROM scan_jobs WHERE plex_guid=?", (plex_guid,))).fetchone()
        return dict(row) if row else None


async def update_scan_job_status(
    plex_guid: str,
    status: str,
    progress: float = 0.0,
    error_msg: str | None = None,
) -> None:
    async with get_connection() as conn:
        if status == "scanning":
            await conn.execute(
                "UPDATE scan_jobs SET status=?, progress=?, started_at=COALESCE(started_at, CURRENT_TIMESTAMP) WHERE plex_guid=?",
                (status, progress, plex_guid),
            )
        elif status in ("done", "failed"):
            await conn.execute(
                "UPDATE scan_jobs SET status=?, progress=?, finished_at=CURRENT_TIMESTAMP, error_msg=? WHERE plex_guid=?",
                (status, progress, error_msg, plex_guid),
            )
        else:
            await conn.execute(
                "UPDATE scan_jobs SET status=?, progress=? WHERE plex_guid=?",
                (status, progress, plex_guid),
            )
        await conn.commit()


async def reset_scan_job(plex_guid: str) -> None:
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE scan_jobs SET status='pending', progress=0, started_at=NULL, finished_at=NULL, error_msg=NULL WHERE plex_guid=?",
            (plex_guid,),
        )
        await conn.commit()


async def set_force_scan(plex_guid: str, force: bool) -> None:
    """Set or unset the force_scan flag for a specific job."""
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE scan_jobs SET force_scan=? WHERE plex_guid=?",
            (1 if force else 0, plex_guid),
        )
        await conn.commit()


async def set_ignored(plex_guid: str, ignored: bool) -> None:
    """Set or unset the ignored flag for a specific job."""
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE scan_jobs SET ignored=? WHERE plex_guid=?",
            (1 if ignored else 0, plex_guid),
        )
        await conn.commit()


async def get_scan_jobs_by_guids(guids: list[str]) -> dict[str, dict]:
    """Return {plex_guid: job} for all provided guids in a single IN query."""
    if not guids:
        return {}
    placeholders = ",".join(["?"] * len(guids))
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"SELECT * FROM scan_jobs WHERE plex_guid IN ({placeholders})",
            guids,
        )
        return {row["plex_guid"]: dict(row) for row in rows}


async def get_scan_jobs_by_library(library_id: str) -> list[dict]:
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM scan_jobs WHERE library_id=? ORDER BY title", (library_id,)
        )
        return [dict(r) for r in rows]


async def get_segment_counts_for_library(library_id: str) -> dict[str, int]:
    """Return {plex_guid: segment_count} for all titles in a library (single query)."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT s.plex_guid, COUNT(*) as cnt
            FROM segments s
            JOIN scan_jobs j ON j.plex_guid = s.plex_guid
            WHERE j.library_id = ?
            GROUP BY s.plex_guid
            """,
            (library_id,),
        )
        return {row["plex_guid"]: row["cnt"] for row in rows}


# ── Segment Library Sharing ────────────────────────────────────────────────────

async def upsert_segment_library_entry(
    file_hash: str,
    file_name: str,
    file_size: int,
    duration_ms: int,
    segments_json: str,
    source_instance: str,
    confidence_level: str = "local",
) -> int:
    """Insert or update a segment library entry with source tracking."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO segment_library_entries(
                file_hash, file_name, file_size, duration_ms, 
                segments_json, source_instance, confidence_level, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(file_hash, source_instance) 
            DO UPDATE SET 
                segments_json=excluded.segments_json,
                confidence_level=excluded.confidence_level,
                updated_at=CURRENT_TIMESTAMP
            """,
            (file_hash, file_name, file_size, duration_ms, segments_json, source_instance, confidence_level),
        )
        await conn.commit()
        return cursor.lastrowid


async def get_segment_library_entries_by_hashes(file_hashes: list[str]) -> list[dict]:
    """Get all library entries matching the given file hashes, grouped by hash."""
    if not file_hashes:
        return []
    
    placeholders = ",".join(["?"] * len(file_hashes))
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"SELECT * FROM segment_library_entries WHERE file_hash IN ({placeholders}) ORDER BY file_hash, created_at DESC",
            file_hashes,
        )
        return [dict(r) for r in rows]


async def get_segment_library_entries_by_hash(file_hash: str) -> list[dict]:
    """Get all sources for a single file hash."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM segment_library_entries WHERE file_hash=? ORDER BY created_at DESC",
            (file_hash,),
        )
        return [dict(r) for r in rows]


async def delete_segment_library_entry(file_hash: str, source_instance: str) -> bool:
    """Delete a specific entry (useful for removing outdated sources)."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "DELETE FROM segment_library_entries WHERE file_hash=? AND source_instance=?",
            (file_hash, source_instance),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def get_sync_metadata() -> dict | None:
    """Get sync configuration for this instance."""
    async with get_connection() as conn:
        row = await (
            await conn.execute(
                "SELECT * FROM sync_metadata LIMIT 1"
            )
        ).fetchone()
        return dict(row) if row else None


async def upsert_sync_metadata(
    instance_name: str,
    github_repo: str | None = None,
    github_token: str | None = None,
    sync_enabled: bool = False,
    conflict_resolution: str = "consensus",
    verified_threshold: int = 2,
    timing_tolerance_ms: int = 2000,
) -> int:
    """Update or create sync metadata for this instance."""
    async with get_connection() as conn:
        # Delete existing if it exists (to update)
        await conn.execute("DELETE FROM sync_metadata")
        
        cursor = await conn.execute(
            """
            INSERT INTO sync_metadata(
                instance_name, github_repo, github_token, sync_enabled,
                conflict_resolution, verified_threshold, timing_tolerance_ms,
                last_sync_time
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                instance_name,
                github_repo,
                github_token,
                1 if sync_enabled else 0,
                conflict_resolution,
                verified_threshold,
                timing_tolerance_ms,
            ),
        )
        await conn.commit()
        return cursor.lastrowid


async def update_sync_last_time() -> None:
    """Update last_sync_time to current timestamp."""
    async with get_connection() as conn:
        await conn.execute("UPDATE sync_metadata SET last_sync_time=CURRENT_TIMESTAMP")
        await conn.commit()


# ── Background Job Management ──────────────────────────────────────────────────

async def create_bg_job(job_type: str) -> int:
    """Create a new background job. Returns job_id."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO bg_jobs(job_type, status, started_at) VALUES(?, ?, CURRENT_TIMESTAMP)",
            (job_type, 'running'),
        )
        await conn.commit()
        return cursor.lastrowid


async def get_bg_job(job_id: int) -> dict | None:
    """Get background job status."""
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT * FROM bg_jobs WHERE id = ?", (job_id,))).fetchone()
        return dict(row) if row else None


async def update_bg_job(job_id: int, status: str = None, progress: int = None, error: str = None, result: str = None) -> None:
    """Update background job status."""
    async with get_connection() as conn:
        updates = []
        params = []
        
        if status is not None:
            updates.append("status = ?")
            params.append(status)
            if status == 'completed':
                updates.append("completed_at = CURRENT_TIMESTAMP")
        
        if progress is not None:
            updates.append("progress_percent = ?")
            params.append(progress)
        
        if error is not None:
            updates.append("error_message = ?")
            params.append(error)
        
        if result is not None:
            updates.append("result_data = ?")
            params.append(result)
        
        if updates:
            params.append(job_id)
            query = f"UPDATE bg_jobs SET {', '.join(updates)} WHERE id = ?"
            await conn.execute(query, params)
            await conn.commit()


async def get_local_library_for_sync() -> list[dict]:
    """Return all locally-scanned titles with their segments, grouped by plex_guid.

    Uses a single JOIN query instead of one query per title to avoid N+1 DB round-trips.
    Returns: [{plex_guid, title, file_path, segments_count, segments: [{...}]}]
    """
    async with get_connection() as conn:
        # Fetch all segment rows for done jobs in a single pass.
        seg_rows = await conn.execute_fetchall(
            """
            SELECT s.*, j.file_path, j.title AS job_title
            FROM segments s
            JOIN scan_jobs j ON j.plex_guid = s.plex_guid
            WHERE j.status = 'done' AND j.file_path IS NOT NULL
            ORDER BY j.title, s.start_ms
            """
        )
        # Also fetch done jobs with no segments so they still appear in the result.
        job_rows = await conn.execute_fetchall(
            """
            SELECT plex_guid, title, file_path
            FROM scan_jobs
            WHERE status = 'done' AND file_path IS NOT NULL
            ORDER BY title
            """
        )

    # Build {plex_guid: {title, file_path, segments: []}} in Python — O(1) per row.
    job_map: dict[str, dict] = {
        r["plex_guid"]: {
            "plex_guid": r["plex_guid"],
            "title": r["title"],
            "file_path": r["file_path"],
            "segments": [],
        }
        for r in job_rows
    }
    for row in seg_rows:
        guid = row["plex_guid"]
        if guid in job_map:
            job_map[guid]["segments"].append(dict(row))

    result = []
    for item in job_map.values():
        item["segments_count"] = len(item["segments"])
        result.append(item)
    return result

