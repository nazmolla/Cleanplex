"""SQLite database access layer using aiosqlite."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .importers._common import CATEGORIES, severity_for_labels
from .logger import get_logger

logger = get_logger(__name__)

_DB_PATH: Path | None = None
# Level a category filters at when a user has no row for it: 3 is "Everything".
# Turning a category down is always an explicit user choice.
DEFAULT_CATEGORY_LEVEL = 3
# Serializes concurrent upsert_plex_markers calls so DELETE+INSERT never interleave.
_marker_upsert_lock = asyncio.Lock()


def set_db_path(path: Path) -> None:
    global _DB_PATH
    _DB_PATH = path


def get_db_path() -> Path:
    if _DB_PATH is None:
        raise RuntimeError("Database path not configured. Call set_db_path() first.")
    return _DB_PATH


BUSY_TIMEOUT_MS = 30_000


@asynccontextmanager
async def get_connection():
    async with aiosqlite.connect(str(get_db_path())) as conn:
        conn.row_factory = aiosqlite.Row
        # foreign_keys, busy_timeout and synchronous are connection-scoped and must be
        # set per connection. journal_mode=WAL is persistent after init_db().
        await conn.execute("PRAGMA foreign_keys=ON")
        # Wait for a competing writer instead of failing at once: scan writes and UI
        # reads overlap, and a busy host can hold a write lock for seconds (#124).
        await conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        # NORMAL is safe in WAL mode (a crash can lose the last commit, never corrupt
        # the file) and avoids an fsync per commit.
        await conn.execute("PRAGMA synchronous=NORMAL")
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

-- User verdicts on detected segments (#101). Kept apart from segments because a
-- rescan deletes and rewrites those; these rows, and the frame copied at mark
-- time, are the sample for tuning thresholds now and retraining models later.
CREATE TABLE IF NOT EXISTS segment_feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plex_guid   TEXT    NOT NULL,
    segment_id  INTEGER,
    verdict     TEXT    NOT NULL DEFAULT 'false_positive',
    start_ms    INTEGER NOT NULL,
    end_ms      INTEGER NOT NULL,
    source      TEXT    DEFAULT '',
    detector    TEXT    DEFAULT '',
    labels      TEXT    DEFAULT '',
    confidence  REAL    DEFAULT 0,
    frame_path  TEXT    DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_segment_feedback_guid ON segment_feedback(plex_guid);

CREATE TABLE IF NOT EXISTS skip_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    plex_guid         TEXT    NOT NULL,
    title             TEXT    DEFAULT '',
    username          TEXT    DEFAULT '',
    client_identifier TEXT    DEFAULT '',
    client_title      TEXT    DEFAULT '',
    category          TEXT    DEFAULT '',
    action            TEXT    DEFAULT 'skip',
    segment_id        INTEGER,
    position_ms       INTEGER DEFAULT 0,
    target_ms         INTEGER DEFAULT 0,
    success           INTEGER DEFAULT 1,
    latency_ms        INTEGER DEFAULT 0,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_skip_events_created ON skip_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_skip_events_client ON skip_events(client_identifier);

CREATE TABLE IF NOT EXISTS user_category_prefs (
    plex_username TEXT    NOT NULL,
    category      TEXT    NOT NULL,
    level         INTEGER DEFAULT 0,
    action        TEXT    DEFAULT '',
    PRIMARY KEY (plex_username, category)
);

-- Whether live skipping can reach a client (a Plex player on the server's network that
-- reports state), learned from the session watcher. The stream proxy leaves such
-- clients alone and edits the stream only for the rest (#132).
CREATE TABLE IF NOT EXISTS client_control (
    client_identifier TEXT PRIMARY KEY,
    controllable      INTEGER NOT NULL,
    last_seen         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_filters (
    plex_username TEXT PRIMARY KEY,
    enabled       INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS scan_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plex_guid   TEXT    UNIQUE NOT NULL,
    title       TEXT,
    file_path   TEXT,
    part_files  TEXT    DEFAULT '',
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

CREATE TABLE IF NOT EXISTS plex_markers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    plex_guid      TEXT    NOT NULL,
    rating_key     TEXT    NOT NULL,
    marker_type    TEXT    NOT NULL,
    start_ms       INTEGER NOT NULL,
    end_ms         INTEGER NOT NULL,
    plex_marker_id INTEGER,
    final          INTEGER DEFAULT 0,
    synced_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(rating_key, plex_marker_id)
);
CREATE INDEX IF NOT EXISTS idx_plex_markers_guid ON plex_markers(plex_guid);
CREATE INDEX IF NOT EXISTS idx_plex_markers_rating_key ON plex_markers(rating_key);

CREATE TABLE IF NOT EXISTS plex_title_cache (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id      TEXT    NOT NULL,
    rating_key      TEXT    NOT NULL,
    plex_guid       TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    media_type      TEXT    NOT NULL,
    show_guid       TEXT    DEFAULT '',
    show_title      TEXT    DEFAULT '',
    show_rating_key TEXT    DEFAULT '',
    content_rating  TEXT    DEFAULT '',
    year            INTEGER,
    cached_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(library_id, rating_key)
);
CREATE INDEX IF NOT EXISTS idx_plex_title_cache_library ON plex_title_cache(library_id);
"""

DEFAULT_SETTINGS = {
    "plex_url": "",
    "plex_token": "",
    "poll_interval": "5",
    "confidence_threshold": "0.6",
    # JSON {label: threshold}; labels not listed inherit the global thresholds.
    "label_thresholds": "{}",
    "siglip_label_thresholds": "{}",
    "ignore_credits": "1",
    "inference_threads": "0",
    "skip_buffer_ms": "3000",
    # Segment bounds are widened by these before matching, so a skip lands ahead of
    # the flagged content and does not re-trigger on the tail. Seeded from
    # skip_buffer_ms at migration time for installs that predate them.
    "pre_buffer_ms": "3000",
    "post_buffer_ms": "3000",
    "scan_step_ms": "5000",
    "scan_workers": "2",
    "segment_gap_ms": "12000",
    "segment_min_hits": "1",
    # Dense re-sample of each flagged window for thumbnail and bounds, in ms; 0 = off.
    "refine_step_ms": "500",
    # SigLIP fallback: off by default because enabling it downloads ~815 MB of
    # model weights on the next scan. Its threshold is a softmax share, not a
    # NudeNet confidence, so it is tuned separately from confidence_threshold.
    # 0.3 from a 238-frame sample of this library: 54% of NudeNet-flagged thumbnails
    # score >= 0.3 (that set includes NudeNet false positives); 1 of 11 ordinary
    # frames did. Biased toward recall, since misses matter more than false hits.
    "siglip_enabled": "0",
    "siglip_threshold": "0.3",
    # Consecutive SigLIP-only frames needed before a segment is written.
    "siglip_min_hits": "2",
    # Use the host's hardware video decoder for scans when it has one (#107).
    "hw_decode": "1",
    "scan_window_start": "23:00",
    "scan_window_end": "06:00",
    # Overnight job rendering hover previews (#114); its own window, set in Settings.
    "hover_previews_enabled": "1",
    "hover_window_start": "00:00",
    "hover_window_end": "07:00",
    "log_level": "INFO",
    "excluded_library_ids": "[]",
    "scan_ratings": "[]",  # empty = scan all ratings
    "scan_labels": "[\"FEMALE_BREAST_EXPOSED\",\"FEMALE_GENITALIA_EXPOSED\",\"MALE_GENITALIA_EXPOSED\",\"ANUS_EXPOSED\",\"BUTTOCKS_EXPOSED\"]",
    "nudenet_model": "320n",
    # Empty means "use subtitle_scanner.DEFAULT_WORDLIST"; a JSON array overrides it.
    "profanity_wordlist": "",
    "nudenet_model_path": "",
    # Segment library sharing settings
    "sync_enabled": "0",
    "sync_instance_name": "",
    "sync_github_repo": "",
    "sync_conflict_resolution": "consensus",
    "sync_verified_threshold": "2",
    "sync_timing_tolerance_ms": "2000",
    # Days of skip history to keep; pruned at startup. 0 disables pruning.
    "skip_event_retention_days": "90",
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
            # Labels a user corrected a segment to (verdict 'relabeled'); `labels` keeps
            # what the detector said, so the pair is a confusion sample (#125).
            "ALTER TABLE segment_feedback ADD COLUMN corrected_labels TEXT DEFAULT ''",
            # Which scanner revision finished the scan (scanner.SCAN_VERSION), so a rescan can
            # be aimed at titles scanned before a change (#138). 0 = scanned before it existed.
            "ALTER TABLE scan_jobs ADD COLUMN scan_version INTEGER DEFAULT 0",
            # Absolute time of the frame saved as the segment's thumbnail (#133); NULL for
            # segments scanned before it was recorded.
            "ALTER TABLE segments ADD COLUMN thumbnail_ms INTEGER",
            # When the backfill job re-sampled a segment stored before dense sampling existed (#141).
            "ALTER TABLE segments ADD COLUMN refined_at TIMESTAMP",
            "ALTER TABLE scan_jobs ADD COLUMN content_rating TEXT DEFAULT ''",
            "ALTER TABLE scan_jobs ADD COLUMN media_type TEXT DEFAULT 'movie'",
            "ALTER TABLE scan_jobs ADD COLUMN force_scan INTEGER DEFAULT 0",
            "ALTER TABLE scan_jobs ADD COLUMN year INTEGER",
            "ALTER TABLE segments ADD COLUMN labels TEXT DEFAULT ''",
            "ALTER TABLE scan_jobs ADD COLUMN ignored INTEGER DEFAULT 0",
            # show_guid stores the Plex grandparentGuid for episodes so the
            # scan queue can group all episodes of a show together.
            "ALTER TABLE scan_jobs ADD COLUMN show_guid TEXT DEFAULT ''",
            # show_rating_key stores the grandparentRatingKey so the library view
            # can build show poster URLs from DB without any Plex API call.
            "ALTER TABLE scan_jobs ADD COLUMN show_rating_key TEXT DEFAULT ''",
            # part_files stores a JSON array of all file paths for multi-part movies
            # (e.g. CD1/CD2 rips). Empty string means single-file title.
            "ALTER TABLE scan_jobs ADD COLUMN part_files TEXT DEFAULT ''",
            # Runtime in milliseconds, used to sanity-check imported skip files
            # against the release actually in the library. 0 means unknown.
            "ALTER TABLE scan_jobs ADD COLUMN duration_ms INTEGER DEFAULT 0",
            # Content classification, following the MovieContentFilter 1.1.0
            # vocabulary. Defaults describe what the NudeNet scanner produces, so
            # rows written before this migration read back correctly.
            "ALTER TABLE segments ADD COLUMN category TEXT DEFAULT 'nudity'",
            "ALTER TABLE segments ADD COLUMN severity TEXT DEFAULT 'high'",
            "ALTER TABLE segments ADD COLUMN action TEXT DEFAULT 'skip'",
            "ALTER TABLE segments ADD COLUMN channel TEXT DEFAULT 'both'",
            "ALTER TABLE segments ADD COLUMN source TEXT DEFAULT 'scanner'",
            "CREATE INDEX IF NOT EXISTS idx_segments_guid_category ON segments(plex_guid, category)",
            # Audio-track language a segment was authored against. Empty means it
            # applies to every track, which is right for image-derived segments.
            "ALTER TABLE segments ADD COLUMN language TEXT DEFAULT ''",
            # Which model produced a scanner segment, e.g. nudenet-640m or
            # siglip-base-224. Empty for imports and rows written before #92.
            "ALTER TABLE segments ADD COLUMN detector TEXT DEFAULT ''",
            # 1 when the user marked the segment as not containing what it claims
            # (#101). Such rows are kept for review but never act or count.
            "ALTER TABLE segments ADD COLUMN false_positive INTEGER DEFAULT 0",
            # Hover preview state (#114): '' not rendered yet, then done, failed or
            # missing (source file unreachable; retried on the next run).
            "ALTER TABLE segments ADD COLUMN hover_status TEXT DEFAULT ''",
            "CREATE INDEX IF NOT EXISTS idx_segments_hover_status ON segments(hover_status)",
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
        # Migrate plex_markers: replace UNIQUE(plex_guid, plex_marker_id) with
        # UNIQUE(rating_key, plex_marker_id). Episodes from the same show share the
        # same IMDB plex_guid, so the old constraint caused false collisions during
        # concurrent bulk sync. plex_markers is a Plex cache — safe to recreate.
        schema_row = await (await conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='plex_markers'"
        )).fetchone()
        if schema_row and "plex_guid, plex_marker_id" in (schema_row["sql"] or ""):
            await conn.executescript("""
                CREATE TABLE IF NOT EXISTS plex_markers_v2 (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    plex_guid      TEXT    NOT NULL,
                    rating_key     TEXT    NOT NULL,
                    marker_type    TEXT    NOT NULL,
                    start_ms       INTEGER NOT NULL,
                    end_ms         INTEGER NOT NULL,
                    plex_marker_id INTEGER,
                    final          INTEGER DEFAULT 0,
                    synced_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(rating_key, plex_marker_id)
                );
                INSERT OR IGNORE INTO plex_markers_v2
                    SELECT * FROM plex_markers;
                DROP TABLE plex_markers;
                ALTER TABLE plex_markers_v2 RENAME TO plex_markers;
                CREATE INDEX IF NOT EXISTS idx_plex_markers_guid ON plex_markers(plex_guid);
                CREATE INDEX IF NOT EXISTS idx_plex_markers_rating_key ON plex_markers(rating_key);
            """)
            await conn.commit()
            logger.info("Migrated plex_markers: UNIQUE constraint now on (rating_key, plex_marker_id)")

        # Seed the split buffers from the existing skip_buffer_ms before the
        # defaults below claim the keys, so an install that tuned skip_buffer_ms
        # keeps its value instead of silently reverting to 3000.
        legacy_buffer = await (await conn.execute(
            "SELECT value FROM settings WHERE key = 'skip_buffer_ms'"
        )).fetchone()
        if legacy_buffer and legacy_buffer["value"]:
            for key in ("pre_buffer_ms", "post_buffer_ms"):
                await conn.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                    (key, legacy_buffer["value"]),
                )

        # Scanner severities used to be a flat high/medium by source; they now follow the
        # detected labels (#126). Re-derive existing detector segments once. Imported
        # segments keep the severity their author set.
        migrated = await (await conn.execute(
            "SELECT 1 FROM settings WHERE key='label_severity_migrated'"
        )).fetchone()
        if not migrated:
            label_sets = await conn.execute_fetchall(
                "SELECT DISTINCT labels FROM segments WHERE source IN ('scanner','siglip') AND labels != ''"
            )
            for row in label_sets:
                await conn.execute(
                    "UPDATE segments SET severity=? WHERE source IN ('scanner','siglip') AND labels=?",
                    (severity_for_labels(row["labels"]), row["labels"]),
                )
            await conn.execute("INSERT INTO settings(key, value) VALUES('label_severity_migrated', '1')")

        for key, value in DEFAULT_SETTINGS.items():
            await conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )
        await conn.commit()
    added = await backfill_user_category_prefs()
    if added:
        logger.info("Backfilled %d missing category prefs at level %d", added, DEFAULT_CATEGORY_LEVEL)
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

async def record_skip_event(
    plex_guid: str,
    title: str = "",
    username: str = "",
    client_identifier: str = "",
    client_title: str = "",
    category: str = "",
    action: str = "skip",
    segment_id: int | None = None,
    position_ms: int = 0,
    target_ms: int = 0,
    success: bool = True,
    latency_ms: int = 0,
) -> int:
    """Record one filter action and return its row id."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO skip_events(plex_guid, title, username, client_identifier, client_title, "
            "category, action, segment_id, position_ms, target_ms, success, latency_ms) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (plex_guid, title, username, client_identifier, client_title, category, action,
             segment_id, position_ms, target_ms, int(success), latency_ms),
        )
        await conn.commit()
        return cursor.lastrowid


def _skip_event_range(start: datetime | None, end: datetime | None) -> tuple[str, list[str]]:
    """Build the WHERE clause for an optional [start, end) window on created_at.

    created_at is SQLite's CURRENT_TIMESTAMP, a naive UTC string, so bounds are
    converted to the same form; naive inputs are taken as already UTC.
    """
    clauses, params = [], []
    for op, bound in ((">=", start), ("<", end)):
        if bound is None:
            continue
        if bound.tzinfo is not None:
            bound = bound.astimezone(timezone.utc).replace(tzinfo=None)
        clauses.append(f"created_at {op} ?")
        params.append(bound.strftime("%Y-%m-%d %H:%M:%S"))
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


async def get_skip_events(
    limit: int = 50,
    offset: int = 0,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict]:
    """Return skip events newest first, optionally limited to [start, end)."""
    where, params = _skip_event_range(start, end)
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"SELECT * FROM skip_events{where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return [dict(r) for r in rows]


async def count_skip_events(start: datetime | None = None, end: datetime | None = None) -> int:
    """Return how many skip events fall in [start, end), or all of them."""
    where, params = _skip_event_range(start, end)
    async with get_connection() as conn:
        row = await (await conn.execute(f"SELECT COUNT(*) FROM skip_events{where}", params)).fetchone()
        return row[0] if row else 0


async def prune_skip_events(retention_days: int) -> int:
    """Delete events older than the retention window. 0 keeps everything."""
    if retention_days <= 0:
        return 0
    async with get_connection() as conn:
        cursor = await conn.execute(
            "DELETE FROM skip_events WHERE created_at < datetime('now', ?)",
            (f"-{int(retention_days)} days",),
        )
        await conn.commit()
        return cursor.rowcount


async def get_skip_counts_by_category() -> list[dict]:
    """Return skip counts grouped by content category, busiest first."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT COALESCE(NULLIF(category, ''), 'unknown') AS category, COUNT(*) AS count "
            "FROM skip_events GROUP BY category ORDER BY count DESC"
        )
        return [dict(r) for r in rows]


async def get_skip_counts_by_client() -> list[dict]:
    """Return per-client volume, failure rate and average latency, worst first.

    Aggregated in SQL rather than by loading rows: this table grows without bound
    between prunes.
    """
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT COALESCE(NULLIF(client_title, ''), 'unknown') AS client_title,
                   client_identifier,
                   COUNT(*)                                   AS count,
                   SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failures,
                   ROUND(AVG(latency_ms))                     AS avg_latency_ms
            FROM skip_events
            GROUP BY client_identifier, client_title
            ORDER BY failures DESC, count DESC
            """
        )
        result = []
        for row in rows:
            item = dict(row)
            item["failure_rate"] = round(item["failures"] / item["count"], 3) if item["count"] else 0.0
            result.append(item)
        return result


async def get_most_skipped_titles(limit: int = 20) -> list[dict]:
    """Return the titles whose segments fire most often."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT plex_guid, COALESCE(NULLIF(title, ''), plex_guid) AS title, COUNT(*) AS count "
            "FROM skip_events GROUP BY plex_guid, title ORDER BY count DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]


async def get_segment_categories() -> list[dict]:
    """Return the categories that actually have segments, with counts."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT COALESCE(NULLIF(category, ''), 'other') AS category, COUNT(*) AS count "
            f"FROM segments WHERE {_ACTIVE} GROUP BY category ORDER BY count DESC"
        )
        return [dict(r) for r in rows]


async def get_user_category_prefs(username: str) -> dict[str, dict]:
    """Return {category: {"level": int, "action": str}} for one user, empty if unset."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT category, level, action FROM user_category_prefs WHERE plex_username=?",
            (username,),
        )
        return {r["category"]: {"level": r["level"], "action": r["action"]} for r in rows}


async def upsert_user_category_pref(username: str, category: str, level: int, action: str = "") -> None:
    """Set one user's filtering level (0-3) and optional action override for a category."""
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO user_category_prefs(plex_username, category, level, action) VALUES(?,?,?,?) "
            "ON CONFLICT(plex_username, category) DO UPDATE SET level=excluded.level, action=excluded.action",
            (username, category, level, action),
        )
        await conn.commit()


async def get_missing_user_category_prefs() -> list[dict]:
    """Return [{"plex_username", "category"}] for categories a user with prefs has no row for."""
    values = ",".join("(?)" for _ in CATEGORIES)
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "WITH users AS (SELECT DISTINCT plex_username FROM user_category_prefs), "
            f"cats(category) AS (VALUES {values}) "
            "SELECT u.plex_username, c.category FROM users u CROSS JOIN cats c "
            "LEFT JOIN user_category_prefs p "
            "  ON p.plex_username = u.plex_username AND p.category = c.category "
            "WHERE p.category IS NULL ORDER BY u.plex_username, c.category",
            CATEGORIES,
        )
        return [dict(r) for r in rows]


async def backfill_user_category_prefs() -> int:
    """Give users with prefs an explicit DEFAULT_CATEGORY_LEVEL row for every missing category.

    Before issue #77 a missing row meant Off, so a user who changed one category
    silently stopped filtering the rest. Rows that already exist are untouched and
    the insert is idempotent. Users with no prefs at all are left alone: an empty
    set already means "filter everything". Returns the number of rows added.
    """
    missing = await get_missing_user_category_prefs()
    if not missing:
        return 0
    async with get_connection() as conn:
        await conn.executemany(
            "INSERT OR IGNORE INTO user_category_prefs(plex_username, category, level, action) "
            "VALUES(?,?,?,'')",
            [(m["plex_username"], m["category"], DEFAULT_CATEGORY_LEVEL) for m in missing],
        )
        await conn.commit()
    return len(missing)


async def delete_user_category_prefs(username: str) -> int:
    """Remove all category preferences for a user, reverting them to defaults."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "DELETE FROM user_category_prefs WHERE plex_username=?", (username,)
        )
        await conn.commit()
        return cursor.rowcount


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

# Segments the user marked as false positives stay in the table for review but are
# excluded from playback, counts, analytics, exports and sync.
_ACTIVE = "COALESCE(false_positive, 0) = 0"
_ACTIVE_S = "COALESCE(s.false_positive, 0) = 0"

async def get_segments_for_guid(plex_guid: str, include_false_positives: bool = False) -> list[dict]:
    """Return a title's segments by start time; marked false positives only if asked."""
    where = "plex_guid=?" if include_false_positives else f"plex_guid=? AND {_ACTIVE}"
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"SELECT * FROM segments WHERE {where} ORDER BY start_ms", (plex_guid,)
        )
        return [dict(r) for r in rows]


async def get_segments_for_guids(plex_guids: list[str], include_false_positives: bool = False) -> list[dict]:
    """Return all segments for multiple guids in a single query, ordered by plex_guid, start_ms."""
    if not plex_guids:
        return []
    active = "" if include_false_positives else f" AND {_ACTIVE}"
    async with get_connection() as conn:
        placeholders = ",".join("?" * len(plex_guids))
        rows = await conn.execute_fetchall(
            f"SELECT * FROM segments WHERE plex_guid IN ({placeholders}){active} ORDER BY plex_guid, start_ms",
            plex_guids,
        )
        return [dict(r) for r in rows]


async def get_segments_for_guid_with_setting(
    plex_guid: str, setting_key: str, setting_default: str
) -> tuple[list[dict], str]:
    """Return segments for a guid and a setting value in a single connection.

    Avoids the cost of opening two aiosqlite connections sequentially when
    both pieces of data are needed together (e.g. segments + scan_labels).
    """
    # Review lists: marked false positives are included so they can be undone.
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM segments WHERE plex_guid=? ORDER BY start_ms", (plex_guid,)
        )
        row = await (
            await conn.execute("SELECT value FROM settings WHERE key=?", (setting_key,))
        ).fetchone()
        setting_val = row["value"] if row else setting_default
        return [dict(r) for r in rows], setting_val


async def get_segments_by_rating_key(rating_key: str) -> list[dict]:
    """Look up segments by scan_jobs.rating_key — fallback when session GUID differs from stored GUID."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """SELECT s.* FROM segments s
               JOIN scan_jobs j ON j.plex_guid = s.plex_guid
               WHERE j.rating_key = ? AND COALESCE(s.false_positive, 0) = 0
               ORDER BY s.start_ms""",
            (rating_key,),
        )
        return [dict(r) for r in rows]


async def get_summary_stats(plex_guid: str) -> dict | None:
    """Return what the Plex summary describes for a title, or None for an unknown title.

    {"job": {status, finished_at, duration_ms, rating_key},
     "segments": active rows (start_ms, end_ms, labels, severity, source),
     "false_positives": count marked, "relabeled": count of corrections kept}.
    """
    async with get_connection() as conn:
        job = await (await conn.execute(
            "SELECT status, finished_at, duration_ms, rating_key FROM scan_jobs WHERE plex_guid=?", (plex_guid,)
        )).fetchone()
        if job is None:
            return None
        rows = await conn.execute_fetchall(
            f"SELECT start_ms, end_ms, labels, severity, source FROM segments WHERE plex_guid=? AND {_ACTIVE} "
            "ORDER BY start_ms",
            (plex_guid,),
        )
        fp = await (await conn.execute(
            "SELECT COUNT(*) FROM segments WHERE plex_guid=? AND false_positive=1", (plex_guid,)
        )).fetchone()
        relabeled = await (await conn.execute(
            "SELECT COUNT(*) FROM segment_feedback WHERE plex_guid=? AND verdict='relabeled'", (plex_guid,)
        )).fetchone()
        return {
            "job": dict(job),
            "segments": [dict(r) for r in rows],
            "false_positives": fp[0],
            "relabeled": relabeled[0],
        }


async def count_segments_for_guid(plex_guid: str) -> int:
    """Return segment count for a title using SELECT COUNT — avoids loading full rows."""
    async with get_connection() as conn:
        row = await (
            await conn.execute(f"SELECT COUNT(*) FROM segments WHERE plex_guid=? AND {_ACTIVE}", (plex_guid,))
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
            f"SELECT * FROM segments WHERE {_ACTIVE} ORDER BY created_at DESC LIMIT ? OFFSET ?",
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
    category: str = "nudity",
    severity: str = "high",
    action: str = "skip",
    channel: str = "both",
    source: str = "scanner",
    detector: str = "",
    thumbnail_ms: int | None = None,
) -> int:
    """Insert one segment and return its row id.

    Classification defaults describe scanner output so existing callers keep
    their behaviour; importers pass the values parsed from the source file.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO segments(plex_guid, title, start_ms, end_ms, confidence, thumbnail_path, "
            "labels, category, severity, action, channel, source, detector, thumbnail_ms) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (plex_guid, title, start_ms, end_ms, confidence, thumbnail_path, labels,
             category, severity, action, channel, source, detector, thumbnail_ms),
        )
        segment_id = cursor.lastrowid
        # A rescan rewrites segments, so a window the user already marked as a false
        # positive would come back. Detector output overlapping such a window starts
        # out marked; imports are the user's own data and are left alone.
        if source in ("scanner", "siglip"):
            await conn.execute(
                "UPDATE segments SET false_positive=1 WHERE id=? AND EXISTS ("
                "SELECT 1 FROM segment_feedback f WHERE f.plex_guid=? AND f.verdict='false_positive' "
                "AND f.start_ms < ? AND f.end_ms > ?)",
                (segment_id, plex_guid, end_ms, start_ms),
            )
            # Same for a label correction: a rescan must not undo the user's relabel.
            fixed = await (await conn.execute(
                "SELECT corrected_labels FROM segment_feedback WHERE plex_guid=? AND verdict='relabeled' "
                "AND corrected_labels != '' AND start_ms < ? AND end_ms > ? ORDER BY id DESC LIMIT 1",
                (plex_guid, end_ms, start_ms),
            )).fetchone()
            if fixed:
                await conn.execute(
                    "UPDATE segments SET labels=?, severity=? WHERE id=?",
                    (fixed["corrected_labels"], severity_for_labels(fixed["corrected_labels"], severity), segment_id),
                )
        await conn.commit()
        return segment_id


async def insert_segments_bulk(plex_guid: str, title: str, segments: list[dict], source: str) -> int:
    """Insert many parsed segments in one transaction and return the row count.

    Used by the importers, where a single file routinely carries 100+ cues —
    one INSERT per cue would be a needless round trip each.
    """
    if not segments:
        return 0
    async with get_connection() as conn:
        await conn.executemany(
            "INSERT INTO segments(plex_guid, title, start_ms, end_ms, confidence, "
            "labels, category, severity, action, channel, language, source) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    plex_guid,
                    title,
                    seg["start_ms"],
                    seg["end_ms"],
                    seg.get("confidence", 1.0),
                    seg.get("labels", ""),
                    seg.get("category", "other"),
                    seg.get("severity", "high"),
                    seg.get("action", "skip"),
                    seg.get("channel", "both"),
                    seg.get("language", ""),
                    source,
                )
                for seg in segments
            ],
        )
        await conn.commit()
        return len(segments)


async def delete_segment(segment_id: int) -> bool:
    async with get_connection() as conn:
        cursor = await conn.execute("DELETE FROM segments WHERE id=?", (segment_id,))
        await conn.commit()
        return cursor.rowcount > 0


async def mark_segment_false_positive(segment_id: int, frame_path: str = "") -> int | None:
    """Flag a segment as a false positive and record the verdict; return the feedback id.

    The feedback row copies the segment's detector, labels, score and window so it
    outlives rescans. Returns None if the segment does not exist. Marking twice
    keeps a single feedback row.
    """
    async with get_connection() as conn:
        seg = await (await conn.execute("SELECT * FROM segments WHERE id=?", (segment_id,))).fetchone()
        if seg is None:
            return None
        existing = await (await conn.execute(
            "SELECT id FROM segment_feedback WHERE segment_id=? AND verdict='false_positive'", (segment_id,)
        )).fetchone()
        await conn.execute("UPDATE segments SET false_positive=1 WHERE id=?", (segment_id,))
        if existing:
            await conn.commit()
            return existing["id"]
        cursor = await conn.execute(
            "INSERT INTO segment_feedback(plex_guid, segment_id, verdict, start_ms, end_ms, source, "
            "detector, labels, confidence, frame_path) VALUES(?,?,'false_positive',?,?,?,?,?,?,?)",
            (seg["plex_guid"], segment_id, seg["start_ms"], seg["end_ms"], seg["source"] or "scanner",
             seg["detector"] or "", seg["labels"] or "", seg["confidence"] or 0, frame_path),
        )
        await conn.commit()
        return cursor.lastrowid


async def relabel_segment(segment_id: int, labels: list[str], frame_path: str = "") -> dict | None:
    """Correct a segment's labels and record the correction; None if the segment is missing.

    Detector segments (scanner, siglip) also get their severity re-derived from the new
    labels; imported ones keep the author's. The first correction stores a 'relabeled'
    feedback row holding the detector's labels and the corrected ones; later edits update
    it, and setting the labels back to the originals removes it (undo). Returns
    {labels, severity, frames}: frames lists copied frames no longer referenced, for the
    caller to delete. frame_path is kept only if it ends up on a new feedback row.
    """
    new = ",".join(dict.fromkeys(labels))
    async with get_connection() as conn:
        seg = await (await conn.execute("SELECT * FROM segments WHERE id=?", (segment_id,))).fetchone()
        if seg is None:
            return None
        fb = await (await conn.execute(
            "SELECT id, labels, frame_path FROM segment_feedback WHERE segment_id=? AND verdict='relabeled'",
            (segment_id,),
        )).fetchone()
        original = fb["labels"] if fb else (seg["labels"] or "")
        severity = seg["severity"]
        if seg["source"] in ("scanner", "siglip"):
            severity = severity_for_labels(new, severity)
        await conn.execute("UPDATE segments SET labels=?, severity=? WHERE id=?", (new, severity, segment_id))

        frames = [frame_path] if frame_path else []
        if sorted(new.split(",")) == sorted(original.split(",")):
            if fb:
                await conn.execute("DELETE FROM segment_feedback WHERE id=?", (fb["id"],))
                frames = [f for f in (fb["frame_path"],) if f] + frames
        elif fb:
            await conn.execute("UPDATE segment_feedback SET corrected_labels=? WHERE id=?", (new, fb["id"]))
        else:
            await conn.execute(
                "INSERT INTO segment_feedback(plex_guid, segment_id, verdict, start_ms, end_ms, source, "
                "detector, labels, corrected_labels, confidence, frame_path) VALUES(?,?,'relabeled',?,?,?,?,?,?,?,?)",
                (seg["plex_guid"], segment_id, seg["start_ms"], seg["end_ms"], seg["source"] or "scanner",
                 seg["detector"] or "", original, new, seg["confidence"] or 0, frame_path),
            )
            frames = []
        await conn.commit()
        return {"labels": new, "severity": severity, "frames": frames}


async def unmark_segment_false_positive(segment_id: int) -> list[str]:
    """Clear a segment's false-positive flag and forget verdicts overlapping its window.

    Returns the frame paths of the removed feedback rows so the caller can delete
    the copied frames. Overlap, not segment id, is used: after a rescan the verdict
    came from an earlier row of the same window.
    """
    async with get_connection() as conn:
        seg = await (await conn.execute("SELECT * FROM segments WHERE id=?", (segment_id,))).fetchone()
        if seg is None:
            return []
        rows = await conn.execute_fetchall(
            "SELECT id, frame_path FROM segment_feedback WHERE plex_guid=? AND verdict='false_positive' "
            "AND (segment_id=? OR (start_ms < ? AND end_ms > ?))",
            (seg["plex_guid"], segment_id, seg["end_ms"], seg["start_ms"]),
        )
        if rows:
            await conn.execute(
                f"DELETE FROM segment_feedback WHERE id IN ({','.join('?' * len(rows))})",
                [r["id"] for r in rows],
            )
        await conn.execute("UPDATE segments SET false_positive=0 WHERE id=?", (segment_id,))
        await conn.commit()
        return [r["frame_path"] for r in rows if r["frame_path"]]


async def get_detection_accuracy() -> list[dict]:
    """Per source/detector/label: segments found, how many were marked false, and scores.

    One row per (source, detector, label) over detector output (scanner/siglip),
    with total, false_positives, and the mean score of kept vs. marked segments, so
    thresholds can be set from real verdicts. A segment with several labels counts
    once per label.
    """
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT COALESCE(NULLIF(s.source, ''), 'scanner') AS source,
                   COALESCE(s.detector, '') AS detector,
                   CASE WHEN TRIM(l.value) = '' THEN '(none)' ELSE TRIM(l.value) END AS label,
                   COUNT(*) AS total,
                   SUM(COALESCE(s.false_positive, 0)) AS false_positives,
                   AVG(CASE WHEN COALESCE(s.false_positive, 0) = 0 THEN s.confidence END) AS avg_score_kept,
                   AVG(CASE WHEN s.false_positive = 1 THEN s.confidence END) AS avg_score_false
            FROM segments s,
                 -- labels is a comma-separated list; split it into one row per label.
                 json_each('["' || REPLACE(COALESCE(s.labels, ''), ',', '","') || '"]') l
            WHERE COALESCE(NULLIF(s.source, ''), 'scanner') IN ('scanner', 'siglip')
            GROUP BY 1, 2, 3
            ORDER BY total DESC
            """
        )
        return [dict(r) for r in rows]


async def get_active_segments_for_rating_key(rating_key: str) -> list[dict]:
    """Return the title's active segments (start_ms, end_ms, category, severity, action), by start.

    Active means not marked a false positive. Every action is returned because a user's
    own action override can turn a mute into a skip. Empty for an unknown rating key.
    """
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT s.start_ms, s.end_ms, s.category, s.severity, s.action "
            "FROM segments s JOIN scan_jobs j ON j.plex_guid = s.plex_guid "
            f"WHERE j.rating_key = ? AND {_ACTIVE_S} ORDER BY s.start_ms",
            (rating_key,),
        )
        return [dict(row) for row in rows]


async def record_client_control(client_identifier: str, controllable: bool) -> None:
    """Remember whether live skipping reaches this client, and when it was last seen."""
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO client_control(client_identifier, controllable) VALUES(?,?) "
            "ON CONFLICT(client_identifier) DO UPDATE SET controllable=excluded.controllable, "
            "last_seen=CURRENT_TIMESTAMP",
            (client_identifier, 1 if controllable else 0),
        )
        await conn.commit()


async def get_client_control(client_identifier: str) -> bool | None:
    """Return whether live skipping reached this client last time, or None if never seen."""
    async with get_connection() as conn:
        row = await (await conn.execute(
            "SELECT controllable FROM client_control WHERE client_identifier=?", (client_identifier,)
        )).fetchone()
        return None if row is None else bool(row["controllable"])


async def get_segment_by_id(segment_id: int) -> dict | None:
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT * FROM segments WHERE id=?", (segment_id,))).fetchone()
        return dict(row) if row else None


_NEEDS_REFINING = (
    "s.source IN ('scanner','siglip') AND s.thumbnail_ms IS NULL AND s.refined_at IS NULL "
    f"AND {_ACTIVE_S}"
)


async def get_segments_to_refine(limit: int) -> list[dict]:
    """Return up to *limit* active detector segments stored before dense sampling, by title then time.

    Rows carry what the backfill needs: the segment, its title's file and parts, and rating key.
    """
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT s.id, s.plex_guid, s.start_ms, s.end_ms, s.confidence, s.labels, s.severity, s.source, "
            "s.detector, s.thumbnail_path, j.file_path, j.part_files, j.title, j.rating_key "
            f"FROM segments s JOIN scan_jobs j ON j.plex_guid = s.plex_guid WHERE {_NEEDS_REFINING} "
            "ORDER BY s.plex_guid, s.start_ms LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]


async def count_segments_to_refine() -> int:
    """Return how many active detector segments are still waiting for the backfill."""
    async with get_connection() as conn:
        row = await (await conn.execute(f"SELECT COUNT(*) FROM segments s WHERE {_NEEDS_REFINING}")).fetchone()
        return row[0]


async def mark_segment_refined(segment_id: int) -> None:
    """Record that the backfill has looked at this segment, so it is never revisited."""
    async with get_connection() as conn:
        await conn.execute("UPDATE segments SET refined_at=CURRENT_TIMESTAMP WHERE id=?", (segment_id,))
        await conn.commit()


async def apply_segment_refinement(
    segment_id: int,
    *,
    start_ms: int,
    end_ms: int,
    confidence: float,
    labels: str,
    severity: str,
    source: str,
    detector: str,
    thumbnail_ms: int | None,
    reset_hover: bool,
) -> None:
    """Store a segment's refined bounds, labels and thumbnail time, and mark it refined.

    reset_hover clears the hover preview state so a segment whose window changed is re-rendered.
    thumbnail_ms is left as it was when the thumbnail was not replaced.
    """
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE segments SET start_ms=?, end_ms=?, confidence=?, labels=?, severity=?, source=?, detector=?, "
            "thumbnail_ms=COALESCE(?, thumbnail_ms), refined_at=CURRENT_TIMESTAMP, "
            "hover_status=CASE WHEN ? THEN '' ELSE hover_status END WHERE id=?",
            (start_ms, end_ms, confidence, labels, severity, source, detector, thumbnail_ms,
             1 if reset_hover else 0, segment_id),
        )
        await conn.commit()


async def get_hover_preview_candidates(limit: int) -> list[dict]:
    """Return up to *limit* active segments without a hover preview, oldest first.

    Each row: id, start_ms, end_ms and the title's file_path. False positives are
    left out; they are not worth the render time.
    """
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"""
            SELECT s.id, s.start_ms, s.end_ms, j.file_path
            FROM segments s
            JOIN scan_jobs j ON j.plex_guid = s.plex_guid
            WHERE COALESCE(s.hover_status, '') = '' AND {_ACTIVE_S}
            ORDER BY s.id
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in rows]


async def set_hover_status(segment_id: int, status: str) -> None:
    """Record a segment's hover preview state: '', 'done', 'failed' or 'missing'."""
    async with get_connection() as conn:
        await conn.execute("UPDATE segments SET hover_status=? WHERE id=?", (status, segment_id))
        await conn.commit()


async def reset_missing_hover_previews() -> int:
    """Make segments whose source was unreachable pending again; return how many."""
    async with get_connection() as conn:
        cur = await conn.execute("UPDATE segments SET hover_status='' WHERE hover_status='missing'")
        await conn.commit()
        return cur.rowcount


async def get_segment_ids() -> set[int]:
    """Return the id of every segment, false positives included."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall("SELECT id FROM segments")
        return {row["id"] for row in rows}


async def get_segments_grouped_by_title() -> list[dict]:
    """Return distinct (plex_guid, title) pairs that have segments."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"SELECT plex_guid, title, COUNT(*) as segment_count FROM segments WHERE {_ACTIVE} "
            "GROUP BY plex_guid ORDER BY title"
        )
        return [dict(r) for r in rows]


# ── Analytics ─────────────────────────────────────────────────────────────────

async def get_segment_counts_by_label() -> list[dict]:
    """Return {label, count} for every label token found in segments.labels.

    Only counts segments that have a matching scan_jobs row so bar-chart totals
    are consistent with what get_segments_for_labels will actually return.
    Orphaned segments (scan_job deleted after scanning) are excluded from both.
    """
    async with get_connection() as conn:
        # JOIN with scan_jobs mirrors the filter used by get_segments_for_labels.
        rows = await conn.execute_fetchall(
            """
            SELECT s.labels FROM segments s
            JOIN scan_jobs j ON j.plex_guid = s.plex_guid
            WHERE s.labels IS NOT NULL AND s.labels != '' AND COALESCE(s.false_positive, 0) = 0
            """
        )
    counts: dict[str, int] = {}
    for row in rows:
        for token in row["labels"].split(","):
            token = token.strip()
            if token:
                counts[token] = counts.get(token, 0) + 1
    return [{"label": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: -x[1])]


async def get_segment_counts_by_rating_for_label(label: str) -> list[dict]:
    """Return {content_rating, count} for segments containing the given label."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT j.content_rating, COUNT(*) as count
            FROM segments s
            JOIN scan_jobs j ON j.plex_guid = s.plex_guid
            WHERE (',' || s.labels || ',') LIKE ? AND COALESCE(s.false_positive, 0) = 0
            GROUP BY j.content_rating
            ORDER BY count DESC
            """,
            (f"%,{label},%",),
        )
    return [{"content_rating": row["content_rating"] or "Unrated", "count": row["count"]} for row in rows]


# Whitelisted ORDER BY columns for the analytics lists; the sort name from a query
# string never reaches SQL except through this map. "marked" applies to false positives.
_SEGMENT_SORT_COLUMNS = {"score": "s.confidence", "added": "s.created_at"}


def _segment_order(sort: str, order: str) -> str:
    """Return an ORDER BY body for sort in score/added and order in asc/desc.

    s.id breaks ties the same way in both directions so OFFSET paging neither
    repeats nor skips rows. Raises ValueError for anything not whitelisted.
    """
    column = _SEGMENT_SORT_COLUMNS.get(sort)
    direction = {"asc": "ASC", "desc": "DESC"}.get(order)
    if column is None or direction is None:
        raise ValueError(f"unsupported sort {sort!r} / order {order!r}")
    return f"{column} {direction}, s.id {direction}"


async def get_segments_for_labels(
    labels: list[str],
    limit: int = 100,
    offset: int = 0,
    sort: str = "score",
    order: str = "desc",
) -> list[dict]:
    """Return segments (with scan_job metadata) that contain any of the given labels.

    Ordered by sort ("score" = confidence, "added" = created_at) and order.
    """
    if not labels:
        return []
    async with get_connection() as conn:
        # Build OR conditions: one LIKE per label token.
        conditions = " OR ".join(["(',' || s.labels || ',') LIKE ?"] * len(labels))
        params = [f"%,{lbl},%" for lbl in labels] + [limit, offset]
        rows = await conn.execute_fetchall(
            f"""
            SELECT s.id, s.plex_guid, s.title, s.start_ms, s.end_ms,
                   s.confidence, s.labels, s.thumbnail_path, s.thumbnail_ms, s.source, s.detector,
                   s.hover_status, j.content_rating, j.media_type, j.rating_key,
                   j.show_rating_key
            FROM segments s
            JOIN scan_jobs j ON j.plex_guid = s.plex_guid
            WHERE ({conditions}) AND COALESCE(s.false_positive, 0) = 0
            ORDER BY {_segment_order(sort, order)}
            LIMIT ? OFFSET ?
            """,
            params,
        )
        return [dict(r) for r in rows]


async def get_false_positive_segments(
    limit: int, offset: int, sort: str = "marked", order: str = "desc"
) -> list[dict]:
    """Return segments marked as false positives, by default most recently marked first.

    sort is "marked" (time of the latest mark), "score" or "added"; order asc/desc.

    Same columns as get_segments_for_labels plus marked_at, the time of the
    latest mark in segment_feedback (NULL for rows marked by a rescan's overlap
    check before feedback rows existed).
    """
    if sort == "marked":
        if order not in ("asc", "desc"):
            raise ValueError(f"unsupported order {order!r}")
        direction = order.upper()
        order_by = f"marked_at IS NULL, marked_at {direction}, s.id {direction}"
    else:
        order_by = _segment_order(sort, order)
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"""
            SELECT s.id, s.plex_guid, s.title, s.start_ms, s.end_ms,
                   s.confidence, s.labels, s.thumbnail_path, s.thumbnail_ms, s.source, s.detector,
                   s.hover_status, j.content_rating, j.media_type, j.rating_key,
                   j.show_rating_key,
                   (SELECT MAX(f.created_at) FROM segment_feedback f
                     WHERE f.segment_id = s.id AND f.verdict = 'false_positive') AS marked_at
            FROM segments s
            JOIN scan_jobs j ON j.plex_guid = s.plex_guid
            WHERE COALESCE(s.false_positive, 0) = 1
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        return [dict(r) for r in rows]


async def count_false_positive_segments() -> int:
    """Return how many segments are marked as false positives."""
    async with get_connection() as conn:
        row = await (await conn.execute(
            "SELECT COUNT(*) FROM segments s JOIN scan_jobs j ON j.plex_guid = s.plex_guid "
            "WHERE COALESCE(s.false_positive, 0) = 1"
        )).fetchone()
        return row[0]


async def count_segments_for_labels(labels: list[str]) -> int:
    """Return total count of segments matching any of the given labels.

    Uses the same JOIN as get_segments_for_labels so the count reflects exactly
    what will be returned — orphaned segments (no matching scan_job) are excluded
    from both, preventing total/results mismatches.
    """
    if not labels:
        return 0
    async with get_connection() as conn:
        conditions = " OR ".join(["(',' || s.labels || ',') LIKE ?"] * len(labels))
        params = [f"%,{lbl},%" for lbl in labels]
        row = await (await conn.execute(
            f"SELECT COUNT(*) FROM segments s JOIN scan_jobs j ON j.plex_guid = s.plex_guid "
            f"WHERE ({conditions}) AND {_ACTIVE_S}",
            params,
        )).fetchone()
        return row[0] if row else 0


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
    show_rating_key: str = "",
    part_files: str = "",
    duration_ms: int = 0,
) -> None:
    async with get_connection() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO scan_jobs"
            "(plex_guid, title, file_path, part_files, rating_key, library_id, library_title, content_rating, media_type, year, show_guid, show_rating_key, duration_ms) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (plex_guid, title, file_path, part_files, rating_key, library_id, library_title, content_rating, media_type, year, show_guid, show_rating_key, duration_ms),
        )
        await conn.commit()


async def get_existing_guids(guids: list[str]) -> set[str]:
    """Return the subset of guids that already exist as scan jobs (single query)."""
    if not guids:
        return set()
    placeholders = ",".join("?" * len(guids))
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"SELECT plex_guid FROM scan_jobs WHERE plex_guid IN ({placeholders})", guids
        )
        return {row["plex_guid"] for row in rows}


async def refresh_scan_job_metadata_batch(
    items: list[tuple[str, str, str, str, str, int | None, str, str, str]],
) -> None:
    """Update mutable Plex metadata for multiple existing scan jobs in a single transaction.

    Each item is (plex_guid, title, file_path, rating_key, content_rating, year, show_guid, show_rating_key, part_files).
    Does not touch scan status or progress.
    """
    if not items:
        return
    async with get_connection() as conn:
        await conn.executemany(
            "UPDATE scan_jobs SET title=?, file_path=?, part_files=?, rating_key=?, content_rating=?, year=?,"
            # Backfill show_guid/show_rating_key from Plex for episodes synced before these columns existed.
            " show_guid=CASE WHEN ? != '' THEN ? ELSE show_guid END,"
            " show_rating_key=CASE WHEN ? != '' THEN ? ELSE show_rating_key END"
            " WHERE plex_guid=?",
            [(title, file_path, part_files, rating_key, content_rating, year,
              show_guid, show_guid, show_rating_key, show_rating_key, plex_guid)
             for plex_guid, title, file_path, rating_key, content_rating, year, show_guid, show_rating_key, part_files in items],
        )
        await conn.commit()


async def delete_scan_jobs_not_in(library_id: str, keep_guids: list[str]) -> int:
    """Delete scan jobs for a library whose plex_guid is not in keep_guids. Returns deleted count."""
    if not keep_guids:
        return 0
    placeholders = ",".join("?" * len(keep_guids))
    async with get_connection() as conn:
        cursor = await conn.execute(
            f"DELETE FROM scan_jobs WHERE library_id=? AND plex_guid NOT IN ({placeholders})",
            [library_id, *keep_guids],
        )
        await conn.commit()
        return cursor.rowcount


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
    scan_version: int | None = None,
) -> None:
    """Set a job's status and progress; a finished scan also records the scanner revision."""
    async with get_connection() as conn:
        if status == "scanning":
            await conn.execute(
                "UPDATE scan_jobs SET status=?, progress=?, started_at=COALESCE(started_at, CURRENT_TIMESTAMP) WHERE plex_guid=?",
                (status, progress, plex_guid),
            )
        elif status in ("done", "failed"):
            await conn.execute(
                "UPDATE scan_jobs SET status=?, progress=?, finished_at=CURRENT_TIMESTAMP, error_msg=?, "
                "scan_version=COALESCE(?, scan_version) WHERE plex_guid=?",
                (status, progress, error_msg, scan_version if status == "done" else None, plex_guid),
            )
        else:
            await conn.execute(
                "UPDATE scan_jobs SET status=?, progress=? WHERE plex_guid=?",
                (status, progress, plex_guid),
            )
        await conn.commit()


RESCAN_SCOPES = ("outdated", "with_segments", "all_scanned", "scanned_before", "whole_library")


async def get_rescan_candidates(
    scope: str,
    current_version: int,
    library_id: str | None = None,
    before: str | None = None,
) -> list[dict]:
    """Return finished, non-ignored scan jobs matching a rescan scope.

    outdated: scanned by an older scanner revision than current_version; with_segments: has at
    least one active segment; all_scanned: every finished title; scanned_before: finished before
    the SQLite timestamp *before*; whole_library: every title of *library_id*, finished or failed.
    Optionally limited to one library. Pending and scanning jobs are never returned (pending ones
    are queued already). Raises ValueError for an unknown scope or a missing date or library.
    """
    if scope not in RESCAN_SCOPES:
        raise ValueError(f"unknown rescan scope {scope!r}")
    if scope == "whole_library" and not library_id:
        raise ValueError("whole_library needs a library")
    where = ["j.status IN ('done', 'failed')" if scope == "whole_library" else "j.status = 'done'",
             "COALESCE(j.ignored, 0) = 0"]
    params: list = []
    if scope == "outdated":
        where.append("COALESCE(j.scan_version, 0) < ?")
        params.append(current_version)
    elif scope == "with_segments":
        where.append(f"EXISTS (SELECT 1 FROM segments s WHERE s.plex_guid = j.plex_guid AND {_ACTIVE_S})")
    elif scope == "scanned_before":
        if not before:
            raise ValueError("scanned_before needs a date")
        where.append("j.finished_at < ?")
        params.append(before)
    if library_id:
        where.append("j.library_id = ?")
        params.append(library_id)
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"SELECT j.plex_guid, j.library_id, j.content_rating, j.ignored FROM scan_jobs j WHERE {' AND '.join(where)}",
            params,
        )
        return [dict(r) for r in rows]


def _like_literal(text: str) -> str:
    """Escape LIKE wildcards so a title such as "100% Wolf" matches literally (ESCAPE '\\')."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def get_show_episode_jobs(
    show_guid: str = "", show_title: str = "", library_id: str | None = None, season: str = ""
) -> list[dict]:
    """Return a show's non-ignored episode jobs, by show GUID or, failing that, by "<show> – " title.

    Some episodes carry no show GUID, so the title prefix ("Show – Season – Episode") is the fallback.
    With *season* (needs show_title) only that season's episodes are returned.
    """
    season_like = f"{_like_literal(show_title)} – {_like_literal(season)} – %" if season and show_title else ""
    async with get_connection() as conn:
        rows: list = []
        if show_guid:
            sql = ("SELECT plex_guid, library_id, content_rating, ignored, status FROM scan_jobs "
                   "WHERE show_guid = ? AND COALESCE(ignored, 0) = 0")
            params = [show_guid]
            if season_like:
                sql += " AND title LIKE ? ESCAPE '\\'"
                params.append(season_like)
            rows = await conn.execute_fetchall(sql, params)
        if not rows and show_title:
            sql = ("SELECT plex_guid, library_id, content_rating, ignored, status FROM scan_jobs "
                   "WHERE media_type = 'episode' AND title LIKE ? ESCAPE '\\' AND COALESCE(ignored, 0) = 0")
            params: list = [season_like or f"{_like_literal(show_title)} – %"]
            if library_id:
                sql += " AND library_id = ?"
                params.append(library_id)
            rows = await conn.execute_fetchall(sql, params)
        return [dict(r) for r in rows]


async def update_part_files(plex_guid: str, part_files: str) -> None:
    """Update the part_files JSON for an existing scan job without touching status or progress."""
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE scan_jobs SET part_files=? WHERE plex_guid=?",
            (part_files, plex_guid),
        )
        await conn.commit()


async def reset_scan_job(plex_guid: str) -> None:
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE scan_jobs SET status='pending', progress=0, started_at=NULL, finished_at=NULL, error_msg=NULL WHERE plex_guid=?",
            (plex_guid,),
        )
        await conn.commit()


async def reset_scan_jobs(plex_guids: list[str]) -> int:
    """Mark many jobs pending (in chunks, within SQLite's variable limit); returns how many matched."""
    total = 0
    async with get_connection() as conn:
        for i in range(0, len(plex_guids), 500):
            chunk = plex_guids[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            cursor = await conn.execute(
                "UPDATE scan_jobs SET status='pending', progress=0, started_at=NULL, finished_at=NULL, "
                f"error_msg=NULL WHERE plex_guid IN ({placeholders})",
                chunk,
            )
            total += cursor.rowcount
        await conn.commit()
    return total


async def reset_interrupted_scans() -> int:
    """Move jobs left in 'scanning' back to 'pending'; returns how many were reset.

    Called once at startup, when no worker can be scanning yet: any such row was
    cut off by a restart and would otherwise show as scanning forever.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE scan_jobs SET status='pending', started_at=NULL WHERE status='scanning'"
        )
        await conn.commit()
        return cursor.rowcount


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
            WHERE j.library_id = ? AND COALESCE(s.false_positive, 0) = 0
            GROUP BY s.plex_guid
            """,
            (library_id,),
        )
        return {row["plex_guid"]: row["cnt"] for row in rows}



async def get_false_positive_counts_for_library(library_id: str) -> dict[str, int]:
    """Return {plex_guid: count} of segments marked false positive, for titles in a library.

    Kept apart from get_segment_counts_for_library so every caller of that one keeps
    counting only segments that act during playback.
    """
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT s.plex_guid, COUNT(*) as cnt
            FROM segments s
            JOIN scan_jobs j ON j.plex_guid = s.plex_guid
            WHERE j.library_id = ? AND COALESCE(s.false_positive, 0) = 1
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
            WHERE j.status = 'done' AND j.file_path IS NOT NULL AND COALESCE(s.false_positive, 0) = 0
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


# ── Plex Markers ───────────────────────────────────────────────────────────────

async def upsert_plex_markers(plex_guid: str, rating_key: str, markers: list[dict]) -> None:
    """Replace all stored markers for a title with the current Plex state.

    Delete-then-insert keyed on rating_key. The module-level lock ensures concurrent
    Sync All calls never interleave their DELETE+INSERT pairs in the same connection.
    """
    async with _marker_upsert_lock:
        async with get_connection() as conn:
            await conn.execute("DELETE FROM plex_markers WHERE rating_key=?", (rating_key,))
            for m in markers:
                await conn.execute(
                    """
                    INSERT INTO plex_markers(plex_guid, rating_key, marker_type, start_ms, end_ms, plex_marker_id, final, synced_at)
                    VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                    """,
                    (plex_guid, rating_key, m["marker_type"], m["start_ms"], m["end_ms"], m["plex_marker_id"], 1 if m.get("final") else 0),
                )
            await conn.commit()


async def get_plex_markers_for_guid(plex_guid: str) -> list[dict]:
    """Return all markers for a title ordered by start_ms."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM plex_markers WHERE plex_guid=? ORDER BY start_ms", (plex_guid,)
        )
        return [dict(r) for r in rows]


async def get_plex_marker(marker_id: int) -> dict | None:
    """Return a single marker row including rating_key and plex_marker_id needed for Plex write-back."""
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT * FROM plex_markers WHERE id=?", (marker_id,))).fetchone()
        return dict(row) if row else None


async def update_plex_marker_timestamps(marker_id: int, start_ms: int, end_ms: int) -> bool:
    """Update start_ms and end_ms for a stored marker. Returns True if a row was updated."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE plex_markers SET start_ms=?, end_ms=? WHERE id=?",
            (start_ms, end_ms, marker_id),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def delete_plex_marker(marker_id: int) -> bool:
    """Delete a single marker row. Returns True if a row was deleted."""
    async with get_connection() as conn:
        cursor = await conn.execute("DELETE FROM plex_markers WHERE id=?", (marker_id,))
        await conn.commit()
        return cursor.rowcount > 0


async def get_plex_marker_counts_for_library(library_id: str) -> dict[str, int]:
    """Return {plex_guid: marker_count} for all titles in a library (single query)."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT m.plex_guid, COUNT(*) as cnt
            FROM plex_markers m
            JOIN scan_jobs j ON j.plex_guid = m.plex_guid
            WHERE j.library_id = ?
            GROUP BY m.plex_guid
            """,
            (library_id,),
        )
        return {row["plex_guid"]: row["cnt"] for row in rows}


async def get_plex_marker_counts_for_guids(guids: list[str]) -> dict[str, int]:
    """Return {plex_guid: marker_count} for an arbitrary list of guids (single query)."""
    if not guids:
        return {}
    placeholders = ",".join("?" * len(guids))
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"SELECT plex_guid, COUNT(*) as cnt FROM plex_markers WHERE plex_guid IN ({placeholders}) GROUP BY plex_guid",
            guids,
        )
        return {row["plex_guid"]: row["cnt"] for row in rows}


async def get_plex_marker_counts_for_rating_keys(rating_keys: list[str]) -> dict[str, int]:
    """Return {rating_key: marker_count} for an arbitrary list of rating keys (single query)."""
    if not rating_keys:
        return {}
    placeholders = ",".join("?" * len(rating_keys))
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"SELECT rating_key, COUNT(*) as cnt FROM plex_markers WHERE rating_key IN ({placeholders}) GROUP BY rating_key",
            rating_keys,
        )
        return {row["rating_key"]: row["cnt"] for row in rows}


async def insert_plex_marker(plex_guid: str, rating_key: str, marker_type: str, start_ms: int, end_ms: int) -> dict:
    """Insert a new manually-created marker (no Plex write). Returns the new row."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO plex_markers(plex_guid, rating_key, marker_type, start_ms, end_ms, plex_marker_id, final, synced_at)
            VALUES(?,?,?,?,?,NULL,0,CURRENT_TIMESTAMP)
            """,
            (plex_guid, rating_key, marker_type, start_ms, end_ms),
        )
        await conn.commit()
        row = await (await conn.execute("SELECT * FROM plex_markers WHERE id=?", (cursor.lastrowid,))).fetchone()
        return dict(row)


async def get_plex_markers_for_rating_key(rating_key: str) -> list[dict]:
    """Return all markers for a Plex item by rating_key, ordered by start_ms."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM plex_markers WHERE rating_key=? ORDER BY start_ms", (rating_key,)
        )
        return [dict(r) for r in rows]


# ── Plex title cache ───────────────────────────────────────────────────────────

async def get_plex_title_cache(library_id: str) -> list[dict]:
    """Return cached title list for a library, joined with current marker counts."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT c.*, COALESCE(m.cnt, 0) AS marker_count
            FROM plex_title_cache c
            LEFT JOIN (
                SELECT rating_key, COUNT(*) AS cnt
                FROM plex_markers
                GROUP BY rating_key
            ) m ON m.rating_key = c.rating_key
            WHERE c.library_id = ?
            ORDER BY c.title
            """,
            (library_id,),
        )
        return [dict(r) for r in rows]


async def upsert_plex_title_cache(library_id: str, items: list[dict]) -> None:
    """Replace the title cache for a library with a fresh Plex snapshot."""
    async with get_connection() as conn:
        await conn.execute("DELETE FROM plex_title_cache WHERE library_id=?", (library_id,))
        for item in items:
            await conn.execute(
                """
                INSERT INTO plex_title_cache
                    (library_id, rating_key, plex_guid, title, media_type,
                     show_guid, show_title, show_rating_key, content_rating, year)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    library_id,
                    item["rating_key"],
                    item["plex_guid"],
                    item["title"],
                    item["media_type"],
                    item.get("show_guid", ""),
                    item.get("show_title", ""),
                    item.get("show_rating_key", ""),
                    item.get("content_rating", ""),
                    item.get("year"),
                ),
            )
        await conn.commit()


async def plex_title_cache_exists(library_id: str) -> bool:
    """Return True if the cache has any rows for this library."""
    async with get_connection() as conn:
        row = await (await conn.execute(
            "SELECT 1 FROM plex_title_cache WHERE library_id=? LIMIT 1", (library_id,)
        )).fetchone()
        return row is not None

