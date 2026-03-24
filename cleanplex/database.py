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
        await conn.execute("PRAGMA journal_mode=WAL")
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
    status      TEXT DEFAULT 'pending',
    progress    REAL DEFAULT 0,
    force_scan  INTEGER DEFAULT 0,
    started_at  TIMESTAMP,
    finished_at TIMESTAMP,
    error_msg   TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

DEFAULT_SETTINGS = {
    "plex_url": "",
    "plex_token": "",
    "poll_interval": "5",
    "confidence_threshold": "0.6",
    "skip_buffer_ms": "3000",
    "scan_step_ms": "5000",
    "segment_gap_ms": "12000",
    "segment_min_hits": "1",
    "scan_window_start": "23:00",
    "scan_window_end": "06:00",
    "log_level": "INFO",
    "excluded_library_ids": "[]",
    "scan_ratings": "[]",  # empty = scan all ratings
    "scan_labels": "[\"FEMALE_BREAST_EXPOSED\",\"FEMALE_GENITALIA_EXPOSED\",\"MALE_GENITALIA_EXPOSED\",\"ANUS_EXPOSED\",\"BUTTOCKS_EXPOSED\"]",
}


async def init_db() -> None:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with get_connection() as conn:
        await conn.executescript(SCHEMA)
        # Migrate: add content_rating column if missing
        try:
            await conn.execute("ALTER TABLE scan_jobs ADD COLUMN content_rating TEXT DEFAULT ''")
            await conn.commit()
        except Exception:
            pass  # column already exists
        # Migrate: add media_type column if missing
        try:
            await conn.execute("ALTER TABLE scan_jobs ADD COLUMN media_type TEXT DEFAULT 'movie'")
            await conn.commit()
        except Exception:
            pass  # column already exists
        # Migrate: add force_scan column if missing
        try:
            await conn.execute("ALTER TABLE scan_jobs ADD COLUMN force_scan INTEGER DEFAULT 0")
            await conn.commit()
        except Exception:
            pass  # column already exists
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
) -> int:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO segments(plex_guid, title, start_ms, end_ms, confidence, thumbnail_path) "
            "VALUES(?,?,?,?,?,?)",
            (plex_guid, title, start_ms, end_ms, confidence, thumbnail_path),
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
) -> None:
    async with get_connection() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO scan_jobs(plex_guid, title, file_path, rating_key, library_id, library_title, content_rating, media_type) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (plex_guid, title, file_path, rating_key, library_id, library_title, content_rating, media_type),
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
