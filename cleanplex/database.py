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
DEFAULT_CATEGORY_LEVEL = 4  # importers._common.MAX_LEVEL: everything
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

-- Per-version state (#233): a Plex title (scan_jobs) can have several media versions (1080p, 4K),
-- each scanned, hashed, indexed and synced with Hayaa independently.
CREATE TABLE IF NOT EXISTS title_versions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    plex_guid        TEXT    NOT NULL,
    plex_media_id    TEXT    DEFAULT '',
    media_index      INTEGER DEFAULT 0,
    resolution       TEXT    DEFAULT '',
    file_path        TEXT    NOT NULL DEFAULT '',
    part_files       TEXT    DEFAULT '',
    part_ids         TEXT    DEFAULT '',
    file_size        INTEGER DEFAULT 0,
    duration_ms      INTEGER DEFAULT 0,
    content_key      TEXT    DEFAULT '',
    content_size     INTEGER DEFAULT 0,
    content_mtime    INTEGER DEFAULT 0,
    status           TEXT    DEFAULT 'pending',
    progress         REAL    DEFAULT 0,
    force_scan       INTEGER DEFAULT 0,
    queue_pos        INTEGER,
    scan_version     INTEGER DEFAULT 0,
    started_at       TIMESTAMP,
    finished_at      TIMESTAMP,
    error_msg        TEXT,
    hayaa_sync_state TEXT    DEFAULT 'pending',
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_title_versions_guid ON title_versions(plex_guid);
CREATE INDEX IF NOT EXISTS idx_title_versions_media ON title_versions(plex_guid, plex_media_id);
CREATE INDEX IF NOT EXISTS idx_title_versions_status ON title_versions(status, queue_pos);
CREATE INDEX IF NOT EXISTS idx_title_versions_content_key ON title_versions(content_key);
CREATE INDEX IF NOT EXISTS idx_title_versions_sync_state ON title_versions(hayaa_sync_state);

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
-- Cast per Plex item (#180). rating_key is the movie's or, for episodes, the show's, so a
-- show's cast is stored once. cast_synced records items already fetched, including those
-- Plex lists no cast for, so they are not asked again until the refresh age passes.
CREATE TABLE IF NOT EXISTS title_cast (
    rating_key TEXT NOT NULL,
    actor      TEXT NOT NULL,
    PRIMARY KEY (rating_key, actor)
);
CREATE INDEX IF NOT EXISTS idx_title_cast_actor ON title_cast(actor);
-- Each actor's photo as Plex lists it (#227): usually a metadata-static.plex.tv URL, shown in the
-- actor filter through Plex's photo transcoder and the /api/plex-image cache.
CREATE TABLE IF NOT EXISTS actor_photos (
    actor TEXT PRIMARY KEY,
    thumb TEXT NOT NULL
);
-- Stream proxy sessions (#184), so an edit survives a restart and live skipping can stand down
-- for streams the proxy already edits.
CREATE TABLE IF NOT EXISTS proxy_sessions (
    session_id  TEXT PRIMARY KEY,
    rating_key  TEXT NOT NULL,
    username    TEXT DEFAULT '',
    client_id   TEXT DEFAULT '',
    client_title TEXT DEFAULT '',
    offset_ms   INTEGER DEFAULT 0,
    ranges      TEXT DEFAULT '[]',
    spans       TEXT DEFAULT '[]',
    removed     TEXT DEFAULT '[]',
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_proxy_sessions_client ON proxy_sessions(client_id, rating_key);

-- Video keyframe times (ms, file time) per media file, so the stream proxy can cut a copied
-- (not re-encoded) stream at real segment boundaries (#184). size/mtime invalidate it.
CREATE TABLE IF NOT EXISTS keyframe_index (
    file_path  TEXT PRIMARY KEY,
    size       INTEGER NOT NULL,
    mtime      INTEGER NOT NULL,
    keyframes  TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Hayaa sync (#222). Titles are identified by their content key (stored on scan_jobs, #235).
-- hayaa_titles: which local title a key belongs to, the consensus version last pulled, and
-- whether its media descriptor was sent.
CREATE TABLE IF NOT EXISTS hayaa_titles (
    title_key  TEXT PRIMARY KEY,
    plex_guid  TEXT NOT NULL,
    version    TEXT DEFAULT '',
    described  INTEGER DEFAULT 0,
    pulled_at  TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_hayaa_titles_guid ON hayaa_titles(plex_guid);
-- Every observation this instance sent. Hayaa ignores a repeated id, so an assertion that is
-- withdrawn and later made again needs a new id: obs_id = base_id plus a generation.
CREATE TABLE IF NOT EXISTS hayaa_sent (
    obs_id     TEXT PRIMARY KEY,
    base_id    TEXT NOT NULL,
    title_key  TEXT NOT NULL,
    kind       TEXT NOT NULL,
    live       INTEGER DEFAULT 1,
    sent_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_hayaa_sent_title ON hayaa_sent(title_key);
CREATE INDEX IF NOT EXISTS idx_hayaa_sent_base ON hayaa_sent(base_id);
-- Pulled consensus claims (staging): nothing here acts until the apply step puts it in segments.
CREATE TABLE IF NOT EXISTS hayaa_claims (
    title_key      TEXT NOT NULL,
    claim_id       TEXT NOT NULL,
    plex_guid      TEXT NOT NULL,
    start_ms       INTEGER NOT NULL,
    end_ms         INTEGER NOT NULL,
    status         TEXT NOT NULL,
    basis          TEXT NOT NULL,
    labels         TEXT DEFAULT '',
    category       TEXT DEFAULT 'nudity',
    severity       TEXT DEFAULT 'high',
    machine        TEXT DEFAULT '{}',
    supporters     INTEGER DEFAULT 0,
    rejecters      INTEGER DEFAULT 0,
    verify_state   TEXT DEFAULT '',
    verify_conf    REAL,
    verify_family  TEXT DEFAULT '',
    verify_fp_drop INTEGER DEFAULT 0,
    verify_thumb   TEXT DEFAULT '',
    verify_labels  TEXT DEFAULT '',
    decision       TEXT DEFAULT '',
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (title_key, claim_id)
);
CREATE INDEX IF NOT EXISTS idx_hayaa_claims_guid ON hayaa_claims(plex_guid);
CREATE INDEX IF NOT EXISTS idx_hayaa_claims_verify ON hayaa_claims(verify_state);
-- The user's own verdict on a pulled claim; always wins over the service (keep, delete, restore).
CREATE TABLE IF NOT EXISTS hayaa_overrides (
    title_key  TEXT NOT NULL,
    claim_id   TEXT NOT NULL,
    verdict    TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (title_key, claim_id)
);

-- Titles or individual versions Plex no longer reports (#191, #233). Their segments (the marks)
-- move to removed_segments, which every live query ignores; everything else is purged after a
-- retention period.
CREATE TABLE IF NOT EXISTS removed_titles (
    version_id    INTEGER PRIMARY KEY,
    plex_guid     TEXT NOT NULL,
    plex_media_id TEXT DEFAULT '',
    resolution    TEXT DEFAULT '',
    title         TEXT,
    rating_key    TEXT,
    file_path     TEXT,
    part_files    TEXT DEFAULT '',
    duration_ms   INTEGER DEFAULT 0,
    status        TEXT DEFAULT '',
    scan_version  INTEGER DEFAULT 0,
    finished_at   TIMESTAMP,
    content_key   TEXT DEFAULT '',
    content_size  INTEGER DEFAULT 0,
    content_mtime INTEGER DEFAULT 0,
    removed_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    purged_at     TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_removed_titles_guid ON removed_titles(plex_guid);
CREATE TABLE IF NOT EXISTS removed_segments (
    id            INTEGER PRIMARY KEY,
    plex_guid     TEXT    NOT NULL,
    version_id    INTEGER DEFAULT 0,
    title         TEXT,
    start_ms      INTEGER NOT NULL,
    end_ms        INTEGER NOT NULL,
    confidence    REAL    DEFAULT 0,
    thumbnail_path TEXT,
    created_at    TIMESTAMP,
    labels        TEXT DEFAULT '',
    category      TEXT DEFAULT 'nudity',
    severity      TEXT DEFAULT 'high',
    action        TEXT DEFAULT 'skip',
    channel       TEXT DEFAULT 'both',
    source        TEXT DEFAULT 'scanner',
    language      TEXT DEFAULT '',
    detector      TEXT DEFAULT '',
    false_positive INTEGER DEFAULT 0,
    hover_status  TEXT DEFAULT '',
    thumbnail_ms  INTEGER,
    refined_at    TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_removed_segments_guid ON removed_segments(plex_guid);

-- Plex part id -> rating key & version, so a direct file request ties to its exact version (#184, #233).
CREATE TABLE IF NOT EXISTS title_parts (
    part_id    INTEGER PRIMARY KEY,
    rating_key TEXT NOT NULL,
    version_id INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cast_synced (
    rating_key TEXT PRIMARY KEY,
    synced_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    # Also render previews whenever the scan queue is empty, not only inside the window.
    "hover_when_idle": "1",
    "verifier_loss_pct": "0",
    # Stream proxy enforcement (#184). Mute segments cannot be muted by editing a playlist:
    # "cut" removes them like skips, "leave" plays them unfiltered on edited streams.
    "enforcement_mute_mode": "leave",
    # Stop a filtered viewer's stream of a flagged title that never passed the proxy.
    "enforcement_backstop": "1",
    # Set when the front door is installed (every client reaches Plex through the proxy).
    # The backstop only acts then: before the cutover every stream bypasses the proxy.
    "enforcement_enabled": "0",
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
            # Hayaa claim a segment belongs to (#222): pulled segments, and local ones matched to a claim.
            "ALTER TABLE segments ADD COLUMN hayaa_claim_id TEXT DEFAULT ''",
            "CREATE INDEX IF NOT EXISTS idx_segments_hover_status ON segments(hover_status)",
            "CREATE INDEX IF NOT EXISTS idx_scan_jobs_status ON scan_jobs(status)",
            # Scan queue position (#174): NULL = not queued; lower runs first. The queue lives
            # in the database so workers claim from it and every view reads the same order.
            "ALTER TABLE scan_jobs ADD COLUMN queue_pos INTEGER",
            # Hayaa sync per title (#231, #235): kept on scan_jobs for legacy compatibility; Step #233
            # moves live sync state and content keys onto title_versions.
            "ALTER TABLE scan_jobs ADD COLUMN content_key TEXT DEFAULT ''",
            "ALTER TABLE scan_jobs ADD COLUMN content_size INTEGER DEFAULT 0",
            "ALTER TABLE scan_jobs ADD COLUMN content_mtime INTEGER DEFAULT 0",
            "ALTER TABLE scan_jobs ADD COLUMN hayaa_sync_state TEXT DEFAULT 'pending'",
            "CREATE INDEX IF NOT EXISTS idx_scan_jobs_sync_state ON scan_jobs(hayaa_sync_state)",
            "CREATE INDEX IF NOT EXISTS idx_scan_jobs_content_key ON scan_jobs(content_key)",
            # Version foreign keys (#233) on per-version tables.
            "ALTER TABLE segments ADD COLUMN version_id INTEGER DEFAULT 0",
            "CREATE INDEX IF NOT EXISTS idx_segments_version ON segments(version_id)",
            "ALTER TABLE removed_segments ADD COLUMN version_id INTEGER DEFAULT 0",
            "CREATE INDEX IF NOT EXISTS idx_removed_segments_version ON removed_segments(version_id)",
            "ALTER TABLE segment_feedback ADD COLUMN version_id INTEGER DEFAULT 0",
            "CREATE INDEX IF NOT EXISTS idx_segment_feedback_version ON segment_feedback(version_id)",
            "ALTER TABLE keyframe_index ADD COLUMN version_id INTEGER DEFAULT 0",
            "CREATE INDEX IF NOT EXISTS idx_keyframe_index_version ON keyframe_index(version_id)",
            "ALTER TABLE hayaa_sent ADD COLUMN version_id INTEGER DEFAULT 0",
            "CREATE INDEX IF NOT EXISTS idx_hayaa_sent_version ON hayaa_sent(version_id)",
            "ALTER TABLE hayaa_claims ADD COLUMN version_id INTEGER DEFAULT 0",
            "CREATE INDEX IF NOT EXISTS idx_hayaa_claims_version ON hayaa_claims(version_id)",
            "ALTER TABLE hayaa_titles ADD COLUMN version_id INTEGER DEFAULT 0",
            "CREATE INDEX IF NOT EXISTS idx_hayaa_titles_version ON hayaa_titles(version_id)",
            "ALTER TABLE title_parts ADD COLUMN version_id INTEGER DEFAULT 0",
            "CREATE INDEX IF NOT EXISTS idx_title_parts_version ON title_parts(version_id)",
            "ALTER TABLE proxy_sessions ADD COLUMN version_id INTEGER DEFAULT 0",
            # What Hayaa sync needs to push the scan of a removed title (#228, #235).
            "ALTER TABLE removed_titles ADD COLUMN plex_media_id TEXT DEFAULT ''",
            "ALTER TABLE removed_titles ADD COLUMN resolution TEXT DEFAULT ''",
            "ALTER TABLE removed_titles ADD COLUMN duration_ms INTEGER DEFAULT 0",
            "ALTER TABLE removed_titles ADD COLUMN status TEXT DEFAULT ''",
            "ALTER TABLE removed_titles ADD COLUMN scan_version INTEGER DEFAULT 0",
            "ALTER TABLE removed_titles ADD COLUMN finished_at TIMESTAMP",
            "ALTER TABLE removed_titles ADD COLUMN content_key TEXT DEFAULT ''",
            "ALTER TABLE removed_titles ADD COLUMN content_size INTEGER DEFAULT 0",
            "ALTER TABLE removed_titles ADD COLUMN content_mtime INTEGER DEFAULT 0",
            # Labels the local re-check found for a pulled claim: Hayaa sends none
            # (ABUSE_RESISTANCE §7), so a pulled segment takes its labels from this server's scan.
            "ALTER TABLE hayaa_claims ADD COLUMN verify_labels TEXT DEFAULT ''",
            "CREATE INDEX IF NOT EXISTS idx_scan_jobs_queue ON scan_jobs(status, queue_pos)",
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
        # Episodes recorded as movies (#246): a row that belongs to a show is an episode. Older
        # versions could store such rows as "movie", and library syncs never rewrote existing rows,
        # so they showed as loose titles instead of under their show. Cheap and idempotent.
        await conn.execute(
            "UPDATE scan_jobs SET media_type='episode' "
            "WHERE media_type != 'episode' AND COALESCE(show_guid, '') != ''"
        )
        await conn.commit()
        # Consolidate hayaa_content_keys into scan_jobs and removed_titles (#235) without rehashing.
        legacy_ck = await (await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hayaa_content_keys'"
        )).fetchone()
        if legacy_ck:
            await conn.execute(
                "UPDATE scan_jobs SET "
                "content_key = COALESCE(NULLIF(content_key, ''), (SELECT k.content_key FROM hayaa_content_keys k WHERE k.file_path = scan_jobs.file_path)), "
                "content_size = COALESCE(NULLIF(content_size, 0), (SELECT k.size FROM hayaa_content_keys k WHERE k.file_path = scan_jobs.file_path)), "
                "content_mtime = COALESCE(NULLIF(content_mtime, 0), (SELECT k.mtime FROM hayaa_content_keys k WHERE k.file_path = scan_jobs.file_path)) "
                "WHERE file_path IN (SELECT file_path FROM hayaa_content_keys)"
            )
            await conn.execute(
                "UPDATE removed_titles SET "
                "content_key = COALESCE(NULLIF(content_key, ''), (SELECT k.content_key FROM hayaa_content_keys k WHERE k.file_path = removed_titles.file_path)), "
                "content_size = COALESCE(NULLIF(content_size, 0), (SELECT k.size FROM hayaa_content_keys k WHERE k.file_path = removed_titles.file_path)), "
                "content_mtime = COALESCE(NULLIF(content_mtime, 0), (SELECT k.mtime FROM hayaa_content_keys k WHERE k.file_path = removed_titles.file_path)) "
                "WHERE file_path IN (SELECT file_path FROM hayaa_content_keys)"
            )
            await conn.execute("DROP TABLE hayaa_content_keys")
            await conn.commit()
        # Upgrade removed_titles from plex_guid PK to version_id PK (#233).
        rt_cols = [r["name"] for r in await conn.execute_fetchall("PRAGMA table_info(removed_titles)")]
        if "version_id" not in rt_cols:
            await conn.executescript("""
                CREATE TABLE removed_titles_v2 (
                    version_id    INTEGER PRIMARY KEY,
                    plex_guid     TEXT NOT NULL,
                    plex_media_id TEXT DEFAULT '',
                    resolution    TEXT DEFAULT '',
                    title         TEXT,
                    rating_key    TEXT,
                    file_path     TEXT,
                    part_files    TEXT DEFAULT '',
                    duration_ms   INTEGER DEFAULT 0,
                    status        TEXT DEFAULT '',
                    scan_version  INTEGER DEFAULT 0,
                    finished_at   TIMESTAMP,
                    content_key   TEXT DEFAULT '',
                    content_size  INTEGER DEFAULT 0,
                    content_mtime INTEGER DEFAULT 0,
                    removed_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    purged_at     TIMESTAMP
                );
                INSERT INTO removed_titles_v2(
                    version_id, plex_guid, plex_media_id, resolution, title, rating_key, file_path,
                    part_files, duration_ms, status, scan_version, finished_at, content_key,
                    content_size, content_mtime, removed_at, purged_at
                )
                SELECT -rowid, plex_guid, COALESCE(plex_media_id, ''), COALESCE(resolution, ''),
                       title, rating_key, file_path, COALESCE(part_files, ''),
                       COALESCE(duration_ms, 0), COALESCE(status, ''), COALESCE(scan_version, 0),
                       finished_at, COALESCE(content_key, ''), COALESCE(content_size, 0),
                       COALESCE(content_mtime, 0), removed_at, purged_at
                FROM removed_titles;
                DROP TABLE removed_titles;
                ALTER TABLE removed_titles_v2 RENAME TO removed_titles;
                CREATE INDEX IF NOT EXISTS idx_removed_titles_guid ON removed_titles(plex_guid);
                UPDATE removed_segments
                SET version_id = COALESCE(
                    (SELECT r.version_id FROM removed_titles r WHERE r.plex_guid = removed_segments.plex_guid LIMIT 1),
                    0
                )
                WHERE COALESCE(version_id, 0) = 0;
            """)
            await conn.commit()
            logger.info("Migrated removed_titles: keyed per removed version")

        # Migrate pre-#233 scan_jobs and dependent tables into title_versions once (and idempotently).
        migrated_versions = await (await conn.execute(
            "SELECT 1 FROM settings WHERE key='versions_v1_migrated'"
        )).fetchone()
        if not migrated_versions:
            sj_cols = {
                r[1] for r in await conn.execute_fetchall("PRAGMA table_info(scan_jobs)")
            }

            def _sj(col: str, fallback: str) -> str:
                return f"j.{col}" if col in sj_cols else fallback

            await conn.execute(f"""
                INSERT INTO title_versions(
                    plex_guid, plex_media_id, media_index, resolution, file_path, part_files, part_ids,
                    file_size, duration_ms, content_key, content_size, content_mtime,
                    status, progress, force_scan, queue_pos, scan_version,
                    started_at, finished_at, error_msg, hayaa_sync_state, created_at
                )
                SELECT
                    j.plex_guid, '', 0, '',
                    COALESCE({_sj('file_path', "''")}, ''),
                    COALESCE({_sj('part_files', "''")}, ''),
                    '', 0,
                    COALESCE({_sj('duration_ms', '0')}, 0),
                    COALESCE({_sj('content_key', "''")}, ''),
                    COALESCE({_sj('content_size', '0')}, 0),
                    COALESCE({_sj('content_mtime', '0')}, 0),
                    COALESCE({_sj('status', "'pending'")}, 'pending'),
                    COALESCE({_sj('progress', '0')}, 0),
                    COALESCE({_sj('force_scan', '0')}, 0),
                    {_sj('queue_pos', 'NULL')},
                    COALESCE({_sj('scan_version', '0')}, 0),
                    {_sj('started_at', 'NULL')},
                    {_sj('finished_at', 'NULL')},
                    {_sj('error_msg', 'NULL')},
                    COALESCE({_sj('hayaa_sync_state', "'pending'")}, 'pending'),
                    {_sj('created_at', 'CURRENT_TIMESTAMP')}
                FROM scan_jobs j
                WHERE NOT EXISTS (SELECT 1 FROM title_versions v WHERE v.plex_guid = j.plex_guid)
            """)
            await conn.executescript("""

                UPDATE segments SET version_id = (
                    SELECT MIN(v.id) FROM title_versions v WHERE v.plex_guid = segments.plex_guid
                ) WHERE COALESCE(version_id, 0) = 0
                  AND EXISTS (SELECT 1 FROM title_versions v WHERE v.plex_guid = segments.plex_guid);

                UPDATE segment_feedback SET version_id = (
                    SELECT MIN(v.id) FROM title_versions v WHERE v.plex_guid = segment_feedback.plex_guid
                ) WHERE COALESCE(version_id, 0) = 0
                  AND EXISTS (SELECT 1 FROM title_versions v WHERE v.plex_guid = segment_feedback.plex_guid);

                UPDATE keyframe_index SET version_id = (
                    SELECT MIN(v.id) FROM title_versions v WHERE v.file_path = keyframe_index.file_path
                ) WHERE COALESCE(version_id, 0) = 0
                  AND EXISTS (SELECT 1 FROM title_versions v WHERE v.file_path = keyframe_index.file_path);

                UPDATE hayaa_titles SET version_id = COALESCE(
                    (SELECT v.id FROM title_versions v WHERE v.content_key = hayaa_titles.title_key LIMIT 1),
                    (SELECT MIN(v.id) FROM title_versions v WHERE v.plex_guid = hayaa_titles.plex_guid),
                    0
                ) WHERE COALESCE(version_id, 0) = 0;

                UPDATE hayaa_claims SET version_id = COALESCE(
                    (SELECT v.id FROM title_versions v WHERE v.content_key = hayaa_claims.title_key LIMIT 1),
                    (SELECT MIN(v.id) FROM title_versions v WHERE v.plex_guid = hayaa_claims.plex_guid),
                    0
                ) WHERE COALESCE(version_id, 0) = 0;

                UPDATE hayaa_sent SET version_id = COALESCE(
                    (SELECT v.id FROM title_versions v WHERE v.content_key = hayaa_sent.title_key LIMIT 1),
                    (SELECT ht.version_id FROM hayaa_titles ht WHERE ht.title_key = hayaa_sent.title_key LIMIT 1),
                    0
                ) WHERE COALESCE(version_id, 0) = 0;

                UPDATE title_parts SET version_id = COALESCE(
                    (SELECT MIN(v.id) FROM title_versions v
                     JOIN scan_jobs j ON j.plex_guid = v.plex_guid
                     WHERE j.rating_key = title_parts.rating_key),
                    0
                ) WHERE COALESCE(version_id, 0) = 0;

                INSERT OR IGNORE INTO settings(key, value) VALUES('versions_v1_migrated', '1');
            """)
            await conn.commit()

        # Triggers marking title_versions.hayaa_sync_state = 'pending' on any shared change (#233, #234).
        # Recreated so upgraded databases replace the legacy scan_jobs triggers cleanly.
        await conn.executescript("""
            DROP TRIGGER IF EXISTS hayaa_pending_scan;
            CREATE TRIGGER hayaa_pending_scan AFTER UPDATE OF status, finished_at, scan_version ON title_versions
            WHEN NEW.status = 'done'
            BEGIN UPDATE title_versions SET hayaa_sync_state = 'pending' WHERE id = NEW.id; END;

            DROP TRIGGER IF EXISTS hayaa_pending_seg_ins;
            CREATE TRIGGER hayaa_pending_seg_ins AFTER INSERT ON segments
            WHEN NEW.source != 'hayaa'
            BEGIN UPDATE title_versions SET hayaa_sync_state = 'pending'
                  WHERE id = CASE WHEN COALESCE(NEW.version_id, 0) > 0 THEN NEW.version_id
                                  ELSE (SELECT MIN(id) FROM title_versions WHERE plex_guid = NEW.plex_guid) END; END;

            DROP TRIGGER IF EXISTS hayaa_pending_seg_upd;
            CREATE TRIGGER hayaa_pending_seg_upd AFTER UPDATE OF false_positive, labels, start_ms, end_ms ON segments
            WHEN NEW.source != 'hayaa' OR NEW.false_positive != OLD.false_positive
            BEGIN UPDATE title_versions SET hayaa_sync_state = 'pending'
                  WHERE id = CASE WHEN COALESCE(NEW.version_id, 0) > 0 THEN NEW.version_id
                                  ELSE (SELECT MIN(id) FROM title_versions WHERE plex_guid = NEW.plex_guid) END; END;

            DROP TRIGGER IF EXISTS hayaa_pending_seg_del;
            CREATE TRIGGER hayaa_pending_seg_del AFTER DELETE ON segments
            WHEN OLD.source != 'hayaa'
            BEGIN UPDATE title_versions SET hayaa_sync_state = 'pending'
                  WHERE id = CASE WHEN COALESCE(OLD.version_id, 0) > 0 THEN OLD.version_id
                                  ELSE (SELECT MIN(id) FROM title_versions WHERE plex_guid = OLD.plex_guid) END; END;

            DROP TRIGGER IF EXISTS hayaa_pending_feedback;
            CREATE TRIGGER hayaa_pending_feedback AFTER INSERT ON segment_feedback
            BEGIN UPDATE title_versions SET hayaa_sync_state = 'pending'
                  WHERE id = CASE WHEN COALESCE(NEW.version_id, 0) > 0 THEN NEW.version_id
                                  ELSE (SELECT MIN(id) FROM title_versions WHERE plex_guid = NEW.plex_guid) END; END;

            DROP TRIGGER IF EXISTS hayaa_pending_override_ins;
            CREATE TRIGGER hayaa_pending_override_ins AFTER INSERT ON hayaa_overrides
            BEGIN UPDATE title_versions SET hayaa_sync_state = 'pending' WHERE content_key = NEW.title_key; END;

            DROP TRIGGER IF EXISTS hayaa_pending_override_upd;
            CREATE TRIGGER hayaa_pending_override_upd AFTER UPDATE ON hayaa_overrides
            BEGIN UPDATE title_versions SET hayaa_sync_state = 'pending' WHERE content_key = NEW.title_key; END;

            DROP TRIGGER IF EXISTS hayaa_pending_override_del;
            CREATE TRIGGER hayaa_pending_override_del AFTER DELETE ON hayaa_overrides
            BEGIN UPDATE title_versions SET hayaa_sync_state = 'pending' WHERE content_key = OLD.title_key; END;

            DROP TRIGGER IF EXISTS hayaa_pending_verify;
            CREATE TRIGGER hayaa_pending_verify AFTER UPDATE OF verify_state ON hayaa_claims
            BEGIN UPDATE title_versions SET hayaa_sync_state = 'pending'
                  WHERE (COALESCE(NEW.version_id, 0) > 0 AND id = NEW.version_id) OR content_key = NEW.title_key; END;
        """)
        await conn.commit()
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

        # Four detector tiers and five viewer levels (#230): re-derive detector severities from
        # labels, and move level 3 (was "everything") to 4 so nobody's filtering loosens.
        tiers = await (await conn.execute(
            "SELECT 1 FROM settings WHERE key='severity_tiers_v2'"
        )).fetchone()
        if not tiers:
            for table in ("segments", "removed_segments"):
                label_sets = await conn.execute_fetchall(
                    f"SELECT DISTINCT labels FROM {table} WHERE source IN ('scanner','siglip','hayaa') AND labels != ''"
                )
                for row in label_sets:
                    await conn.execute(
                        f"UPDATE {table} SET severity=? WHERE source IN ('scanner','siglip','hayaa') AND labels=?",
                        (severity_for_labels(row["labels"]), row["labels"]),
                    )
            await conn.execute("UPDATE user_category_prefs SET level = 4 WHERE level = 3")
            await conn.execute("INSERT INTO settings(key, value) VALUES('severity_tiers_v2', '1')")

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
    """Set one user's filtering level (0-4) and optional action override for a category."""
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
# segments.false_positive: 0 acts, 1 marked false by the user, 2 rejected by the verifier (#165).
# Both non-zero states are inactive; only 1 is a human verdict.
FP_MARKED = 1
FP_VERIFIER = 2
# Hidden because Hayaa's community rejected the claim (#222); restorable like the others.
FP_COMMUNITY = 3
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


async def get_segments_for_version(version_id: int, include_false_positives: bool = False) -> list[dict]:
    """Return segments belonging to one media version by start time."""
    where = "version_id=?" if include_false_positives else f"version_id=? AND {_ACTIVE}"
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"SELECT * FROM segments WHERE {where} ORDER BY start_ms", (version_id,)
        )
        return [dict(r) for r in rows]


async def get_segments_for_versions(version_ids: list[int], include_false_positives: bool = False) -> list[dict]:
    """Return segments belonging to multiple media versions, ordered by version_id, start_ms."""
    if not version_ids:
        return []
    active = "" if include_false_positives else f" AND {_ACTIVE}"
    async with get_connection() as conn:
        rows: list = []
        for i in range(0, len(version_ids), 500):
            chunk = version_ids[i:i + 500]
            rows += await conn.execute_fetchall(
                f"SELECT * FROM segments WHERE version_id IN ({','.join('?' * len(chunk))}){active} "
                "ORDER BY version_id, start_ms",
                chunk,
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


async def get_segments_for_guids_with_setting(
    plex_guids: list[str], setting_key: str, setting_default: str
) -> tuple[list[dict], str]:
    """Return every segment (false positives included) for the guids plus one setting, in a single connection."""
    async with get_connection() as conn:
        rows: list = []
        for i in range(0, len(plex_guids), 500):  # chunked: SQLite's variable limit
            chunk = plex_guids[i:i + 500]
            rows += await conn.execute_fetchall(
                f"SELECT * FROM segments WHERE plex_guid IN ({','.join('?' * len(chunk))}) ORDER BY plex_guid, start_ms",
                chunk,
            )
        row = await (await conn.execute("SELECT value FROM settings WHERE key=?", (setting_key,))).fetchone()
        return [dict(r) for r in rows], row["value"] if row else setting_default


async def fail_unfinished_bg_jobs(error_message: str) -> int:
    """Mark every running or queued background job failed with *error_message*; return how many."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE bg_jobs SET status='failed', error_message=? WHERE status IN ('running', 'queued')",
            (error_message,),
        )
        await conn.commit()
        return cursor.rowcount


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


async def _resolve_version_id_conn(
    conn,
    *,
    rating_key: str = "",
    plex_guid: str = "",
    part_id: int = 0,
    plex_media_id: str = "",
    file_path: str = "",
) -> tuple[int | None, bool]:
    """Resolve a session's media version id and report whether the title has any recorded versions.

    Returns ``(version_id, has_versions)``. Matches strictly by part_id, plex_media_id, or
    file_path so segments from one version are never served for another.
    """
    if part_id:
        row = await (await conn.execute(
            "SELECT version_id FROM title_parts WHERE part_id=? AND version_id > 0",
            (part_id,),
        )).fetchone()
        if row:
            return int(row["version_id"]), True

    if plex_guid:
        vcount = (await (await conn.execute(
            "SELECT COUNT(*) FROM title_versions WHERE plex_guid=?", (plex_guid,)
        )).fetchone())[0]
        if vcount > 0:
            if plex_media_id:
                row = await (await conn.execute(
                    "SELECT id FROM title_versions WHERE plex_guid=? AND plex_media_id=? LIMIT 1",
                    (plex_guid, str(plex_media_id)),
                )).fetchone()
                if row:
                    return int(row["id"]), True
            if file_path:
                row = await (await conn.execute(
                    "SELECT id FROM title_versions WHERE plex_guid=? AND file_path=? LIMIT 1",
                    (plex_guid, file_path),
                )).fetchone()
                if row:
                    return int(row["id"]), True
            return None, True

    if rating_key:
        vcount = (await (await conn.execute(
            "SELECT COUNT(*) FROM title_versions v JOIN scan_jobs j ON j.plex_guid = v.plex_guid "
            "WHERE j.rating_key=?",
            (rating_key,),
        )).fetchone())[0]
        if vcount > 0:
            if plex_media_id:
                row = await (await conn.execute(
                    "SELECT v.id FROM title_versions v JOIN scan_jobs j ON j.plex_guid = v.plex_guid "
                    "WHERE j.rating_key=? AND v.plex_media_id=? LIMIT 1",
                    (rating_key, str(plex_media_id)),
                )).fetchone()
                if row:
                    return int(row["id"]), True
            if file_path:
                row = await (await conn.execute(
                    "SELECT v.id FROM title_versions v JOIN scan_jobs j ON j.plex_guid = v.plex_guid "
                    "WHERE j.rating_key=? AND v.file_path=? LIMIT 1",
                    (rating_key, file_path),
                )).fetchone()
                if row:
                    return int(row["id"]), True
            return None, True

    if plex_media_id:
        row = await (await conn.execute(
            "SELECT id FROM title_versions WHERE plex_media_id=? LIMIT 1",
            (str(plex_media_id),),
        )).fetchone()
        if row:
            return int(row["id"]), True
    if file_path:
        row = await (await conn.execute(
            "SELECT id FROM title_versions WHERE file_path=? LIMIT 1",
            (file_path,),
        )).fetchone()
        if row:
            return int(row["id"]), True
    return None, False


async def resolve_session_version(
    *,
    rating_key: str = "",
    plex_guid: str = "",
    part_id: int = 0,
    plex_media_id: str = "",
    file_path: str = "",
) -> int | None:
    """Resolve the title_versions.id for a Plex playback session without mixing versions."""
    async with get_connection() as conn:
        vid, _ = await _resolve_version_id_conn(
            conn,
            rating_key=rating_key,
            plex_guid=plex_guid,
            part_id=part_id,
            plex_media_id=plex_media_id,
            file_path=file_path,
        )
        return vid


async def get_segments_for_session(
    *,
    rating_key: str = "",
    plex_guid: str = "",
    part_id: int = 0,
    plex_media_id: str = "",
    file_path: str = "",
) -> list[dict]:
    """Return active segments for the exact Plex version being played.

    When the session identifies a media version (part_id, plex_media_id, or file_path) and
    versions are recorded for the title, only that version's segments are returned so cuts from
    another edition/resolution never bleed into playback. Falls back to guid/rating_key only
    when no version is identified or no version rows exist.
    """
    async with get_connection() as conn:
        identifies_version = bool(part_id or plex_media_id or file_path)
        vid, has_versions = await _resolve_version_id_conn(
            conn,
            rating_key=rating_key,
            plex_guid=plex_guid,
            part_id=part_id,
            plex_media_id=plex_media_id,
            file_path=file_path,
        )
        if vid is not None:
            rows = await conn.execute_fetchall(
                f"SELECT * FROM segments WHERE version_id=? AND {_ACTIVE} ORDER BY start_ms",
                (vid,),
            )
            return [dict(r) for r in rows]
        if identifies_version and has_versions:
            return []
        if plex_guid:
            rows = await conn.execute_fetchall(
                f"SELECT * FROM segments WHERE plex_guid=? AND {_ACTIVE} ORDER BY start_ms",
                (plex_guid,),
            )
            if rows:
                return [dict(r) for r in rows]
        if rating_key:
            rows = await conn.execute_fetchall(
                """SELECT s.* FROM segments s
                   JOIN scan_jobs j ON j.plex_guid = s.plex_guid
                   WHERE j.rating_key = ? AND COALESCE(s.false_positive, 0) = 0
                   ORDER BY s.start_ms""",
                (rating_key,),
            )
            return [dict(r) for r in rows]
        return []


async def get_summary_stats(plex_guid: str) -> dict | None:
    """Return what the Plex summary describes for a title, or None for an unknown title.

    {"job": {status, finished_at, duration_ms, rating_key},
     "segments": active rows (start_ms, end_ms, labels, severity, source),
     "false_positives": count marked, "relabeled": count of corrections kept}.
    """
    async with get_connection() as conn:
        job = await (await conn.execute(
            """SELECT j.status,
                      COALESCE(NULLIF(j.finished_at, ''), MAX(v.finished_at)) AS finished_at,
                      CASE WHEN COALESCE(j.duration_ms, 0) > 0 THEN j.duration_ms ELSE COALESCE(MAX(v.duration_ms), 0) END AS duration_ms,
                      j.rating_key
               FROM scan_jobs j
               LEFT JOIN title_versions v ON v.plex_guid = j.plex_guid
               WHERE j.plex_guid=?
               GROUP BY j.plex_guid""",
            (plex_guid,),
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


async def get_segment_file_ref(segment_id: int) -> tuple[int, str] | None:
    """Return (id, thumbnail_path) of a segment, or None if it does not exist."""
    async with get_connection() as conn:
        row = await (await conn.execute(
            "SELECT id, COALESCE(thumbnail_path, '') FROM segments WHERE id=?", (segment_id,)
        )).fetchone()
        return (row[0], row[1]) if row else None


async def get_segment_file_refs_for_guid(plex_guid: str) -> list[tuple[int, str]]:
    """Return (id, thumbnail_path) of every segment of a title, so their files can be removed with them."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT id, COALESCE(thumbnail_path, '') FROM segments WHERE plex_guid=?", (plex_guid,)
        )
        return [(r[0], r[1]) for r in rows]


async def get_segment_file_refs_for_version(version_id: int) -> list[tuple[int, str]]:
    """Return (id, thumbnail_path) of every segment of one version, so their files can be removed."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT id, COALESCE(thumbnail_path, '') FROM segments WHERE version_id=?", (version_id,)
        )
        return [(r[0], r[1]) for r in rows]


async def get_referenced_thumbnail_paths() -> set[str]:
    """Return every thumbnail path some segment points at, archived marks included (the sweep keeps these files)."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT thumbnail_path FROM segments WHERE COALESCE(thumbnail_path, '') != '' "
            "UNION SELECT thumbnail_path FROM removed_segments WHERE COALESCE(thumbnail_path, '') != ''"
        )
        return {r[0] for r in rows}


async def get_feedback_frame_paths() -> set[str]:
    """Return every frame path a feedback row owns (false positives and relabels keep their frame)."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall("SELECT frame_path FROM segment_feedback WHERE COALESCE(frame_path, '') != ''")
        return {r[0] for r in rows}


async def get_verifier_training_rows(max_kept_per_group: int) -> list[dict]:
    """Return labelled frames for the false-positive verifier as {path, fp, group, plex_guid}.

    fp=1: frames users rejected (feedback verdict false_positive). fp=0: frames of segments the
    user kept (a random sample per group) and frames of relabeled feedback, which were real
    nudity under the wrong label (taken under the corrected label). group is
    "<family>:<label>": family "siglip" or "nudenet" by the detector that fired, label the
    first (peak-frame) label. A label has its own group so what was rejected under one label,
    for instance while it was disabled, cannot teach the model to reject another (#178).
    """
    def group_of(detector: str, labels: str) -> str | None:
        label = (labels or "").split(",")[0].strip()
        if not label:
            return None
        return f"{'siglip' if detector.startswith('siglip') else 'nudenet'}:{label}"

    rows: list[dict] = []
    async with get_connection() as conn:
        feedback = await conn.execute_fetchall(
            "SELECT frame_path, verdict, detector, plex_guid, labels, corrected_labels FROM segment_feedback "
            "WHERE frame_path != '' AND (detector LIKE 'siglip%' OR detector LIKE 'nudenet%')"
        )
        for r in feedback:
            relabeled = r["verdict"] != "false_positive"
            group = group_of(r["detector"], (r["corrected_labels"] if relabeled else r["labels"]) or "")
            if group:
                rows.append({"path": r["frame_path"], "fp": 0 if relabeled else 1, "group": group, "plex_guid": r["plex_guid"]})
        counts: dict[str, int] = {}
        for like in ("siglip%", "nudenet%"):
            kept = await conn.execute_fetchall(
                "SELECT thumbnail_path, plex_guid, detector, labels FROM segments WHERE false_positive=0 "
                "AND thumbnail_path != '' AND detector LIKE ? ORDER BY RANDOM()",
                (like,),
            )
            for r in kept:
                group = group_of(r["detector"], r["labels"])
                if group and counts.get(group, 0) < max_kept_per_group:
                    counts[group] = counts.get(group, 0) + 1
                    rows.append({"path": r["thumbnail_path"], "fp": 0, "group": group, "plex_guid": r["plex_guid"]})
    return rows


# Columns shared by segments and removed_segments, in the order they are copied between them.
_SEGMENT_COLS = (
    "id, plex_guid, version_id, title, start_ms, end_ms, confidence, thumbnail_path, created_at, labels, category, severity, "
    "action, channel, source, language, detector, false_positive, hover_status, thumbnail_ms, refined_at"
)


def _in(values: list) -> str:
    return ",".join("?" * len(values))


async def archive_segments(conn, guids: list[str]) -> None:
    """Move the segments of *guids* into removed_segments and note each title in removed_titles.

    A move, not a delete: the rows (the marks) are kept, the files they point to stay until the
    purge, and no live query sees them. Runs inside the caller's transaction. The titles named by
    *guids* must still have their scan_jobs row, or have been recorded in removed_titles already.
    """
    for i in range(0, len(guids), 500):
        chunk = guids[i:i + 500]
        await conn.execute(
            f"INSERT OR REPLACE INTO removed_segments({_SEGMENT_COLS}) SELECT {_SEGMENT_COLS} FROM segments WHERE plex_guid IN ({_in(chunk)})",
            chunk,
        )
        await conn.execute(f"DELETE FROM segments WHERE plex_guid IN ({_in(chunk)})", chunk)


async def archive_version_segments(conn, version_ids: list[int]) -> None:
    """Move the segments of *version_ids* into removed_segments inside the caller's transaction."""
    for i in range(0, len(version_ids), 500):
        chunk = version_ids[i:i + 500]
        await conn.execute(
            f"INSERT OR REPLACE INTO removed_segments({_SEGMENT_COLS}) SELECT {_SEGMENT_COLS} FROM segments WHERE version_id IN ({_in(chunk)})",
            chunk,
        )
        await conn.execute(f"DELETE FROM segments WHERE version_id IN ({_in(chunk)})", chunk)


async def remove_titles_not_in(library_id: str, keep_guids: list[str]) -> int:
    """Remove a library's titles that Plex no longer reports; return how many titles were removed.

    Each title's versions and segments are archived (kept as marks, hidden everywhere) and
    recorded in removed_titles per version with the data needed to purge later. An empty
    *keep_guids* removes nothing, so a failed Plex listing cannot wipe a library.
    """
    if not keep_guids:
        return 0
    async with get_connection() as conn:
        jobs = await conn.execute_fetchall(
            f"SELECT plex_guid, title, rating_key, file_path, part_files, COALESCE(duration_ms, 0) AS duration_ms, "
            f"status, COALESCE(scan_version, 0) AS scan_version, finished_at, "
            f"COALESCE(content_key, '') AS content_key, COALESCE(content_size, 0) AS content_size, "
            f"COALESCE(content_mtime, 0) AS content_mtime FROM scan_jobs "
            f"WHERE library_id=? AND plex_guid NOT IN ({_in(keep_guids)})",
            [library_id, *keep_guids],
        )
        if not jobs:
            return 0
        guids = [r["plex_guid"] for r in jobs]
        v_rows: list = []
        for i in range(0, len(guids), 500):
            chunk = guids[i:i + 500]
            v_rows += await conn.execute_fetchall(
                f"SELECT v.id AS version_id, v.plex_guid, j.title, j.rating_key, v.file_path, v.part_files, "
                f"COALESCE(v.duration_ms, 0) AS duration_ms, v.status, COALESCE(v.scan_version, 0) AS scan_version, "
                f"v.finished_at, COALESCE(v.content_key, '') AS content_key, "
                f"COALESCE(v.content_size, 0) AS content_size, COALESCE(v.content_mtime, 0) AS content_mtime "
                f"FROM title_versions v JOIN scan_jobs j ON j.plex_guid = v.plex_guid "
                f"WHERE v.plex_guid IN ({_in(chunk)})",
                chunk,
            )
        covered_guids = {r["plex_guid"] for r in v_rows}
        if v_rows:
            await conn.executemany(
                "INSERT OR REPLACE INTO removed_titles(version_id, plex_guid, title, rating_key, file_path, part_files, "
                "duration_ms, status, scan_version, finished_at, content_key, content_size, content_mtime) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(r["version_id"], r["plex_guid"], r["title"], r["rating_key"], r["file_path"], r["part_files"] or "",
                  r["duration_ms"], r["status"] or "", r["scan_version"], r["finished_at"],
                  r["content_key"], r["content_size"], r["content_mtime"]) for r in v_rows],
            )
        legacy_jobs = [r for r in jobs if r["plex_guid"] not in covered_guids]
        if legacy_jobs:
            await conn.executemany(
                "INSERT OR IGNORE INTO removed_titles(plex_guid, title, rating_key, file_path, part_files, "
                "duration_ms, status, scan_version, finished_at, content_key, content_size, content_mtime) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [(r["plex_guid"], r["title"], r["rating_key"], r["file_path"] or "", r["part_files"] or "",
                  r["duration_ms"], r["status"] or "", r["scan_version"], r["finished_at"],
                  r["content_key"], r["content_size"], r["content_mtime"]) for r in legacy_jobs],
            )
        await archive_segments(conn, guids)
        for i in range(0, len(guids), 500):
            chunk = guids[i:i + 500]
            await conn.execute(f"DELETE FROM title_versions WHERE plex_guid IN ({_in(chunk)})", chunk)
            await conn.execute(f"DELETE FROM scan_jobs WHERE plex_guid IN ({_in(chunk)})", chunk)
        await conn.commit()
        return len(guids)


async def archive_orphan_segments(older_than_days: int = 1) -> int:
    """Archive segments whose title has no scan job (left behind by older syncs); return how many.

    The age cutoff keeps a segment written moments before its job exists out of the sweep.
    """
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT plex_guid, COALESCE(version_id, 0) AS version_id, MIN(title) AS title, COUNT(*) AS n "
            "FROM segments s "
            "WHERE NOT EXISTS (SELECT 1 FROM scan_jobs j WHERE j.plex_guid = s.plex_guid) "
            "AND s.created_at < datetime('now', ?) GROUP BY plex_guid, COALESCE(version_id, 0)",
            (f"-{int(older_than_days)} days",),
        )
        if not rows:
            return 0
        for r in rows:
            if r["version_id"]:
                await conn.execute(
                    "INSERT OR IGNORE INTO removed_titles(version_id, plex_guid, title, file_path) VALUES(?,?,?,'')",
                    (r["version_id"], r["plex_guid"], r["title"]),
                )
            else:
                await conn.execute(
                    "INSERT OR IGNORE INTO removed_titles(plex_guid, title, file_path) VALUES(?,?,'')",
                    (r["plex_guid"], r["title"]),
                )
        guids = list({r["plex_guid"] for r in rows})
        # Only the old rows move: a fresh segment of the same guid stays live until it, too, ages out.
        for i in range(0, len(guids), 500):
            chunk = guids[i:i + 500]
            await conn.execute(
                f"INSERT OR REPLACE INTO removed_segments({_SEGMENT_COLS}) SELECT {_SEGMENT_COLS} FROM segments "
                f"WHERE plex_guid IN ({_in(chunk)}) AND created_at < datetime('now', ?)",
                [*chunk, f"-{int(older_than_days)} days"],
            )
            await conn.execute(
                f"DELETE FROM segments WHERE plex_guid IN ({_in(chunk)}) AND created_at < datetime('now', ?)",
                [*chunk, f"-{int(older_than_days)} days"],
            )
        await conn.commit()
        return sum(r["n"] for r in rows)


async def restore_removed_titles(guids: list[str]) -> int:
    """Move the archived marks of titles that came back into segments; return how many were restored."""
    total = 0
    async with get_connection() as conn:
        for i in range(0, len(guids), 500):
            chunk = guids[i:i + 500]
            # Carry cached content keys back onto matching live versions.
            await conn.execute(
                "UPDATE title_versions SET "
                "content_key = COALESCE((SELECT r.content_key FROM removed_titles r WHERE r.plex_guid = title_versions.plex_guid AND r.file_path = title_versions.file_path AND COALESCE(r.content_key, '') != '' LIMIT 1), content_key), "
                "content_size = COALESCE((SELECT r.content_size FROM removed_titles r WHERE r.plex_guid = title_versions.plex_guid AND r.file_path = title_versions.file_path AND COALESCE(r.content_key, '') != '' LIMIT 1), content_size), "
                "content_mtime = COALESCE((SELECT r.content_mtime FROM removed_titles r WHERE r.plex_guid = title_versions.plex_guid AND r.file_path = title_versions.file_path AND COALESCE(r.content_key, '') != '' LIMIT 1), content_mtime) "
                f"WHERE plex_guid IN ({_in(chunk)}) AND COALESCE(content_key, '') = ''",
                chunk,
            )
            cursor = await conn.execute(
                f"INSERT OR IGNORE INTO segments({_SEGMENT_COLS}) SELECT {_SEGMENT_COLS} FROM removed_segments WHERE plex_guid IN ({_in(chunk)})",
                chunk,
            )
            total += cursor.rowcount
            # Re-link restored segments to the current live version_id for that guid/file.
            for g in chunk:
                live_versions = await conn.execute_fetchall(
                    "SELECT id, file_path FROM title_versions WHERE plex_guid=? ORDER BY id", (g,)
                )
                if not live_versions:
                    continue
                if len(live_versions) == 1:
                    await conn.execute(
                        "UPDATE segments SET version_id=? WHERE plex_guid=? "
                        "AND (COALESCE(version_id, 0) = 0 OR NOT EXISTS (SELECT 1 FROM title_versions v WHERE v.id = segments.version_id))",
                        (live_versions[0]["id"], g),
                    )
                else:
                    old_rows = await conn.execute_fetchall(
                        "SELECT version_id, file_path FROM removed_titles WHERE plex_guid=?", (g,)
                    )
                    by_path = {v["file_path"]: v["id"] for v in live_versions if v["file_path"]}
                    for r in old_rows:
                        new_vid = by_path.get(r["file_path"])
                        if new_vid:
                            await conn.execute(
                                "UPDATE segments SET version_id=? WHERE plex_guid=? AND version_id=?",
                                (new_vid, g, r["version_id"]),
                            )
            await conn.execute(f"DELETE FROM removed_segments WHERE plex_guid IN ({_in(chunk)})", chunk)
            await conn.execute(f"DELETE FROM removed_titles WHERE plex_guid IN ({_in(chunk)})", chunk)
        await conn.commit()
    return total


async def get_removed_segments(plex_guid: str) -> list[dict]:
    """Return the archived marks of a removed title, by start time."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall("SELECT * FROM removed_segments WHERE plex_guid=? ORDER BY start_ms", (plex_guid,))
        return [dict(r) for r in rows]


async def get_removed_title(plex_guid: str) -> dict | None:
    """Return a removed title's record, or None."""
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT * FROM removed_titles WHERE plex_guid=? LIMIT 1", (plex_guid,))).fetchone()
        return dict(row) if row else None


async def purge_removed_titles(older_than_days: int) -> list[tuple[int, str]]:
    """Delete what is saved for removed versions/titles older than the cutoff, keeping their marks.

    Deletes skip events, Plex markers and the cached title row when no live version of that guid
    remains, and cast, parts, proxy sessions and keyframe indexes where no live title/version still
    uses that rating key or file (a re-matched title keeps both). Archived segments keep their
    marks with thumbnail_path and hover_status cleared. Returns (segment id, thumbnail path) for
    the purged archived segments so the caller can delete their files. Each entry is purged once.
    """
    async with get_connection() as conn:
        titles = await conn.execute_fetchall(
            "SELECT version_id, plex_guid, rating_key, file_path, part_files FROM removed_titles "
            "WHERE purged_at IS NULL AND removed_at < datetime('now', ?)",
            (f"-{int(older_than_days)} days",),
        )
        if not titles:
            return []
        version_ids = [t["version_id"] for t in titles]
        live_jobs = await conn.execute_fetchall("SELECT plex_guid, rating_key FROM scan_jobs")
        live_guids = {r["plex_guid"] for r in live_jobs if r["plex_guid"]}
        live_keys = {r["rating_key"] for r in live_jobs if r["rating_key"]}
        live_vers = await conn.execute_fetchall("SELECT file_path, part_files FROM title_versions")
        live_files: set[str] = set()
        for r in live_vers:
            live_files.add(r["file_path"] or "")
            live_files.update(_json_list(r["part_files"]))
        dead_guids = sorted({t["plex_guid"] for t in titles} - live_guids)
        keys = sorted({t["rating_key"] for t in titles if t["rating_key"]} - live_keys)
        files = sorted({f for t in titles for f in [t["file_path"] or "", *_json_list(t["part_files"])] if f} - live_files)

        refs: list[tuple[int, str]] = []
        # Thumbnail cleanup on archived segments belonging to these removed versions (or fully removed guids).
        for i in range(0, len(version_ids), 500):
            vchunk = version_ids[i:i + 500]
            rows = await conn.execute_fetchall(
                f"SELECT id, COALESCE(thumbnail_path, '') AS t FROM removed_segments WHERE version_id IN ({_in(vchunk)})",
                vchunk,
            )
            refs += [(r["id"], r["t"]) for r in rows]
            await conn.execute(
                f"UPDATE removed_segments SET thumbnail_path='', hover_status='' WHERE version_id IN ({_in(vchunk)})",
                vchunk,
            )
            for table in ("hayaa_claims", "hayaa_titles"):
                await conn.execute(
                    f"DELETE FROM {table} WHERE version_id IN ({_in(vchunk)}) "
                    f"AND NOT EXISTS (SELECT 1 FROM title_versions v WHERE v.id = {table}.version_id)",
                    vchunk,
                )
        for i in range(0, len(dead_guids), 500):
            chunk = dead_guids[i:i + 500]
            rows = await conn.execute_fetchall(
                f"SELECT id, COALESCE(thumbnail_path, '') AS t FROM removed_segments "
                f"WHERE plex_guid IN ({_in(chunk)}) AND COALESCE(thumbnail_path, '') != ''",
                chunk,
            )
            seen = {r_id for r_id, _ in refs}
            refs += [(r["id"], r["t"]) for r in rows if r["id"] not in seen]
            await conn.execute(
                f"UPDATE removed_segments SET thumbnail_path='', hover_status='' WHERE plex_guid IN ({_in(chunk)})",
                chunk,
            )
            await conn.execute(f"DELETE FROM skip_events WHERE plex_guid IN ({_in(chunk)})", chunk)
            await conn.execute(f"DELETE FROM plex_markers WHERE plex_guid IN ({_in(chunk)})", chunk)
            await conn.execute(
                f"DELETE FROM plex_title_cache WHERE plex_guid IN ({_in(chunk)})", chunk
            )
            for table in ("hayaa_claims", "hayaa_titles"):
                await conn.execute(
                    f"DELETE FROM {table} WHERE plex_guid IN ({_in(chunk)})",
                    chunk,
                )
            await conn.execute(f"DELETE FROM title_versions WHERE plex_guid IN ({_in(chunk)})", chunk)
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            for table in ("title_cast", "cast_synced", "title_parts", "proxy_sessions"):
                await conn.execute(f"DELETE FROM {table} WHERE rating_key IN ({_in(chunk)})", chunk)
        for i in range(0, len(files), 500):
            chunk = files[i:i + 500]
            await conn.execute(f"DELETE FROM keyframe_index WHERE file_path IN ({_in(chunk)})", chunk)
        for i in range(0, len(version_ids), 500):
            vchunk = version_ids[i:i + 500]
            # A content key nothing was sent for yet is kept: Hayaa sync can still push the
            # removed version's scan under it (#228, #235).
            await conn.execute(
                f"UPDATE removed_titles SET content_key='', content_size=0, content_mtime=0 "
                f"WHERE version_id IN ({_in(vchunk)}) "
                "AND EXISTS (SELECT 1 FROM hayaa_sent s WHERE s.title_key = removed_titles.content_key)",
                vchunk,
            )
            await conn.execute(f"UPDATE removed_titles SET purged_at=CURRENT_TIMESTAMP WHERE version_id IN ({_in(vchunk)})", vchunk)
        await conn.commit()
        return refs


def _json_list(raw: str | None) -> list[str]:
    """Parse a JSON list column, tolerating blanks and bad data."""
    try:
        value = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


async def delete_segments_by_ids(segment_ids: list[int]) -> int:
    """Delete segments by id (in chunks within SQLite's variable limit); return how many were deleted."""
    total = 0
    async with get_connection() as conn:
        for i in range(0, len(segment_ids), 500):
            chunk = segment_ids[i:i + 500]
            cursor = await conn.execute(
                f"DELETE FROM segments WHERE id IN ({','.join('?' * len(chunk))})", chunk
            )
            total += cursor.rowcount
        await conn.commit()
    return total


async def delete_segments_for_guid(plex_guid: str) -> int:
    """Delete all stored segments for a title and return deleted row count."""
    async with get_connection() as conn:
        cursor = await conn.execute("DELETE FROM segments WHERE plex_guid=?", (plex_guid,))
        await conn.commit()
        return cursor.rowcount


async def delete_segments_for_version(version_id: int) -> int:
    """Delete all stored segments for one media version and return deleted row count."""
    async with get_connection() as conn:
        cursor = await conn.execute("DELETE FROM segments WHERE version_id=?", (version_id,))
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
    false_positive: int = 0,
    version_id: int | None = None,
) -> int:
    """Insert one segment and return its row id; false_positive 2 stores a verifier rejection (inactive, undoable).

    Classification defaults describe scanner output so existing callers keep
    their behaviour; importers pass the values parsed from the source file.
    """
    async with get_connection() as conn:
        vid = version_id
        if vid is None:
            vrow = await (await conn.execute(
                "SELECT id FROM title_versions WHERE plex_guid=? ORDER BY id LIMIT 1", (plex_guid,)
            )).fetchone()
            vid = int(vrow["id"]) if vrow else 0
        cursor = await conn.execute(
            "INSERT INTO segments(plex_guid, version_id, title, start_ms, end_ms, confidence, thumbnail_path, "
            "labels, category, severity, action, channel, source, detector, thumbnail_ms, false_positive) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (plex_guid, vid, title, start_ms, end_ms, confidence, thumbnail_path, labels,
             category, severity, action, channel, source, detector, thumbnail_ms, false_positive),
        )
        segment_id = cursor.lastrowid
        # A rescan rewrites segments, so a window the user already marked as a false
        # positive would come back. Detector output overlapping such a window starts
        # out marked; imports are the user's own data and are left alone.
        if source in ("scanner", "siglip"):
            await conn.execute(
                "UPDATE segments SET false_positive=1 WHERE id=? AND EXISTS ("
                "SELECT 1 FROM segment_feedback f WHERE f.plex_guid=? AND f.verdict='false_positive' "
                "AND (COALESCE(f.version_id, 0) = ? OR COALESCE(f.version_id, 0) = 0 OR ? = 0) "
                "AND f.start_ms < ? AND f.end_ms > ?)",
                (segment_id, plex_guid, vid, vid, end_ms, start_ms),
            )
            # Same for a label correction: a rescan must not undo the user's relabel.
            fixed = await (await conn.execute(
                "SELECT corrected_labels FROM segment_feedback WHERE plex_guid=? AND verdict='relabeled' "
                "AND (COALESCE(version_id, 0) = ? OR COALESCE(version_id, 0) = 0 OR ? = 0) "
                "AND corrected_labels != '' AND start_ms < ? AND end_ms > ? ORDER BY id DESC LIMIT 1",
                (plex_guid, vid, vid, end_ms, start_ms),
            )).fetchone()
            if fixed:
                await conn.execute(
                    "UPDATE segments SET labels=?, severity=? WHERE id=?",
                    (fixed["corrected_labels"], severity_for_labels(fixed["corrected_labels"], severity), segment_id),
                )
        await conn.commit()
        return segment_id


async def insert_segments_bulk(
    plex_guid: str,
    title: str,
    segments: list[dict],
    source: str,
    version_id: int | None = None,
) -> int:
    """Insert many parsed segments in one transaction and return the row count.

    Used by the importers, where a single file routinely carries 100+ cues —
    one INSERT per cue would be a needless round trip each.
    """
    if not segments:
        return 0
    async with get_connection() as conn:
        vid = version_id
        if vid is None:
            vrow = await (await conn.execute(
                "SELECT id FROM title_versions WHERE plex_guid=? ORDER BY id LIMIT 1", (plex_guid,)
            )).fetchone()
            vid = int(vrow["id"]) if vrow else 0
        await conn.executemany(
            "INSERT INTO segments(plex_guid, version_id, title, start_ms, end_ms, confidence, "
            "labels, category, severity, action, channel, language, source) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    plex_guid,
                    seg.get("version_id", vid) or 0,
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
            "INSERT INTO segment_feedback(plex_guid, version_id, segment_id, verdict, start_ms, end_ms, source, "
            "detector, labels, confidence, frame_path) VALUES(?,?,?,'false_positive',?,?,?,?,?,?,?)",
            (seg["plex_guid"], seg["version_id"] or 0, segment_id, seg["start_ms"], seg["end_ms"],
             seg["source"] or "scanner", seg["detector"] or "", seg["labels"] or "", seg["confidence"] or 0, frame_path),
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
                "INSERT INTO segment_feedback(plex_guid, version_id, segment_id, verdict, start_ms, end_ms, source, "
                "detector, labels, corrected_labels, confidence, frame_path) VALUES(?,?,?,'relabeled',?,?,?,?,?,?,?,?)",
                (seg["plex_guid"], seg["version_id"] or 0, segment_id, seg["start_ms"], seg["end_ms"],
                 seg["source"] or "scanner", seg["detector"] or "", original, new, seg["confidence"] or 0, frame_path),
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
        vid = seg["version_id"] or 0
        rows = await conn.execute_fetchall(
            "SELECT id, frame_path FROM segment_feedback WHERE plex_guid=? AND verdict='false_positive' "
            "AND (COALESCE(version_id, 0) = ? OR COALESCE(version_id, 0) = 0 OR ? = 0) "
            "AND (segment_id=? OR (start_ms < ? AND end_ms > ?))",
            (seg["plex_guid"], vid, vid, segment_id, seg["end_ms"], seg["start_ms"]),
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
                   SUM(CASE WHEN s.false_positive = 1 THEN 1 ELSE 0 END) AS false_positives,
                   SUM(CASE WHEN s.false_positive = 2 THEN 1 ELSE 0 END) AS verifier_rejected,
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
    """Return the title's active segments (id, plex_guid, title, start_ms, end_ms, category, severity, action), by start.

    Active means not marked a false positive. Every action is returned because a user's
    own action override can turn a mute into a skip. Empty for an unknown rating key.
    """
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT s.id, s.plex_guid, j.title, s.start_ms, s.end_ms, s.category, s.severity, s.action "
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

    Rows carry what the backfill needs: the segment, its version's file and parts, and rating key.
    """
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT s.id, s.plex_guid, s.version_id, s.start_ms, s.end_ms, s.confidence, s.labels, s.severity, s.source, "
            "s.detector, s.thumbnail_path, COALESCE(v.file_path, j.file_path) AS file_path, "
            "COALESCE(NULLIF(v.part_files, ''), j.part_files) AS part_files, j.title, j.rating_key "
            "FROM segments s LEFT JOIN title_versions v ON v.id = s.version_id "
            f"LEFT JOIN scan_jobs j ON j.plex_guid = s.plex_guid WHERE {_NEEDS_REFINING} "
            "AND (v.file_path IS NOT NULL OR j.file_path IS NOT NULL) "
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

    Each row: id, start_ms, end_ms and the version's file_path and part_files. False positives
    are left out; they are not worth the render time.
    """
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"""
            SELECT s.id, s.start_ms, s.end_ms,
                   COALESCE(v.file_path, j.file_path) AS file_path,
                   COALESCE(NULLIF(v.part_files, ''), j.part_files) AS part_files
            FROM segments s
            LEFT JOIN title_versions v ON v.id = s.version_id
            LEFT JOIN scan_jobs j ON j.plex_guid = s.plex_guid
            WHERE COALESCE(s.hover_status, '') = '' AND {_ACTIVE_S}
              AND (v.file_path IS NOT NULL OR j.file_path IS NOT NULL)
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
    """Return the id of every segment, false positives and archived marks included."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall("SELECT id FROM segments UNION SELECT id FROM removed_segments")
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


_FP_KIND_SQL = {"all": "IN (1, 2)", "marked": "= 1", "verifier": "= 2"}


async def get_false_positive_segments(
    limit: int, offset: int, sort: str = "marked", order: str = "desc", kind: str = "all"
) -> list[dict]:
    """Return segments marked as false positives, by default most recently marked first.

    sort is "marked" (time of the latest mark), "score" or "added"; order asc/desc.

    kind is "all", "marked" (by the user) or "verifier" (rejected by the verifier); fp_state is 1 or 2.

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
                   j.show_rating_key, s.false_positive AS fp_state,
                   (SELECT MAX(f.created_at) FROM segment_feedback f
                     WHERE f.segment_id = s.id AND f.verdict = 'false_positive') AS marked_at
            FROM segments s
            JOIN scan_jobs j ON j.plex_guid = s.plex_guid
            WHERE COALESCE(s.false_positive, 0) {_FP_KIND_SQL[kind]}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        return [dict(r) for r in rows]


async def count_false_positive_segments(kind: str = "all") -> int:
    """Return how many segments are marked as false positives (kind as in get_false_positive_segments)."""
    async with get_connection() as conn:
        row = await (await conn.execute(
            "SELECT COUNT(*) FROM segments s JOIN scan_jobs j ON j.plex_guid = s.plex_guid "
            f"WHERE COALESCE(s.false_positive, 0) {_FP_KIND_SQL[kind]}"
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
    plex_media_id: str = "",
    resolution: str = "",
    file_size: int = 0,
    part_ids: str = "",
) -> None:
    """Insert a scan job if missing and ensure it has at least one title_versions row."""
    async with get_connection() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO scan_jobs"
            "(plex_guid, title, file_path, part_files, rating_key, library_id, library_title, content_rating, media_type, year, show_guid, show_rating_key, duration_ms) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (plex_guid, title, file_path, part_files, rating_key, library_id, library_title, content_rating, media_type, year, show_guid, show_rating_key, duration_ms),
        )
        has_v = await (await conn.execute(
            "SELECT 1 FROM title_versions WHERE plex_guid=? LIMIT 1", (plex_guid,)
        )).fetchone()
        if not has_v and file_path:
            await conn.execute(
                "INSERT INTO title_versions(plex_guid, plex_media_id, media_index, resolution, file_path, part_files, part_ids, file_size, duration_ms) "
                "VALUES(?,?,0,?,?,?,?,?,?)",
                (plex_guid, plex_media_id, resolution, file_path, part_files, part_ids, file_size, duration_ms),
            )
        await conn.commit()


async def get_failed_jobs_for_library(library_id: str) -> list[dict]:
    """Return a library's failed, non-ignored scan version jobs (version + parent job columns)."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT j.*, v.id AS version_id, v.plex_media_id, v.media_index, v.resolution, "
            "v.file_path, v.part_files, v.part_ids, v.file_size, v.duration_ms, "
            "v.status, v.progress, v.force_scan, v.queue_pos, v.scan_version, "
            "v.started_at, v.finished_at, v.error_msg "
            "FROM title_versions v JOIN scan_jobs j ON j.plex_guid = v.plex_guid "
            "WHERE j.library_id=? AND v.status='failed' AND COALESCE(j.ignored, 0)=0",
            (library_id,),
        )
        return [dict(r) for r in rows]


async def _refresh_job_summary_conn(conn, guids: list[str]) -> None:
    """Keep scan_jobs status/timestamps synced from its title_versions rows."""
    for g in set(guids):
        rows = await conn.execute_fetchall(
            "SELECT status, progress, force_scan, scan_version, started_at, finished_at, error_msg, "
            "file_path, part_files, duration_ms FROM title_versions WHERE plex_guid=? ORDER BY media_index, id",
            (g,),
        )
        if not rows:
            continue
        statuses = [r["status"] or "pending" for r in rows]
        if any(s == "scanning" for s in statuses):
            summary = "scanning"
        elif all(s == "done" for s in statuses):
            summary = "done"
        elif any(s == "pending" for s in statuses):
            summary = "pending"
        elif any(s == "failed" for s in statuses):
            summary = "failed"
        else:
            summary = statuses[0]
        primary = rows[0]
        finished = max((r["finished_at"] for r in rows if r["finished_at"]), default=None)
        started = min((r["started_at"] for r in rows if r["started_at"]), default=None)
        force = 1 if any(r["force_scan"] for r in rows) else 0
        sv = max((r["scan_version"] or 0 for r in rows), default=0)
        err = next((r["error_msg"] for r in rows if r["status"] == "failed" and r["error_msg"]), None)
        prog = sum(float(r["progress"] or 0.0) for r in rows) / len(rows)
        await conn.execute(
            "UPDATE scan_jobs SET status=?, progress=?, force_scan=?, scan_version=?, "
            "started_at=?, finished_at=?, error_msg=?, file_path=?, part_files=?, duration_ms=? "
            "WHERE plex_guid=?",
            (summary, prog, force, sv, started, finished, err,
             primary["file_path"], primary["part_files"], primary["duration_ms"], g),
        )


async def upsert_scan_jobs_batch(jobs: list[dict]) -> int:
    """Insert scan jobs that do not exist yet, in one transaction; return how many were inserted.

    Each dict takes the keyword arguments of upsert_scan_job (plus optional ``versions`` list);
    existing guids are left untouched.
    """
    if not jobs:
        return 0
    rows = [
        (
            j["plex_guid"], j["title"], j["file_path"], j.get("part_files", ""), j["rating_key"],
            j["library_id"], j["library_title"], j.get("content_rating", ""), j.get("media_type", "movie"),
            j.get("year"), j.get("show_guid", ""), j.get("show_rating_key", ""), j.get("duration_ms", 0),
        )
        for j in jobs
    ]
    async with get_connection() as conn:
        before = conn.total_changes
        await conn.executemany(
            "INSERT OR IGNORE INTO scan_jobs"
            "(plex_guid, title, file_path, part_files, rating_key, library_id, library_title, content_rating, media_type, year, show_guid, show_rating_key, duration_ms) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        await conn.commit()
        inserted = conn.total_changes - before

        # Existing rows keep everything else, but which show a title belongs to is Plex's to say:
        # refresh it, so a row stored with the wrong type or no show heals on the next sync (#246).
        show_rows = [
            (j["show_guid"], j.get("show_rating_key", ""), j["plex_guid"],
             j["show_guid"], j.get("show_rating_key", ""))
            for j in jobs if j.get("show_guid")
        ]
        if show_rows:
            await conn.executemany(
                "UPDATE scan_jobs SET media_type='episode', show_guid=?, show_rating_key=? "
                "WHERE plex_guid=? AND (media_type != 'episode' OR COALESCE(show_guid, '') != ? "
                "OR COALESCE(show_rating_key, '') != ?)",
                show_rows,
            )
            await conn.commit()

        # Ensure titles inserted via legacy callers without explicit sync_title_versions_batch still
        # get a title_versions row.
        guids = [j["plex_guid"] for j in jobs]
        existing_v: set[str] = set()
        for i in range(0, len(guids), 500):
            chunk = guids[i:i + 500]
            erows = await conn.execute_fetchall(
                f"SELECT DISTINCT plex_guid FROM title_versions WHERE plex_guid IN ({_in(chunk)})", chunk
            )
            existing_v.update(r["plex_guid"] for r in erows)
        to_insert: list[tuple] = []
        for j in jobs:
            if j["plex_guid"] in existing_v:
                continue
            existing_v.add(j["plex_guid"])
            versions = j.get("versions")
            if versions:
                for idx, v in enumerate(versions):
                    to_insert.append((
                        j["plex_guid"],
                        str(v.get("plex_media_id", "")),
                        v.get("media_index", idx),
                        v.get("resolution", ""),
                        v.get("file_path", ""),
                        v.get("part_files", ""),
                        v.get("part_ids", ""),
                        v.get("file_size", 0),
                        v.get("duration_ms", 0),
                    ))
            elif j.get("file_path"):
                to_insert.append((
                    j["plex_guid"],
                    str(j.get("plex_media_id", "")),
                    0,
                    j.get("resolution", ""),
                    j["file_path"],
                    j.get("part_files", ""),
                    j.get("part_ids", ""),
                    j.get("file_size", 0),
                    j.get("duration_ms", 0),
                ))
        if to_insert:
            await conn.executemany(
                "INSERT INTO title_versions(plex_guid, plex_media_id, media_index, resolution, file_path, "
                "part_files, part_ids, file_size, duration_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                to_insert,
            )
            await conn.commit()
    # A title that returns gets its archived marks back (#191).
    await restore_removed_titles([j["plex_guid"] for j in jobs])
    return inserted


async def get_title_versions(plex_guid: str) -> list[dict]:
    """Return every media version for one title, ordered by media_index, id."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM title_versions WHERE plex_guid=? ORDER BY media_index, id",
            (plex_guid,),
        )
        return [dict(r) for r in rows]


async def get_title_version(version_id: int) -> dict | None:
    """Return one media version joined with its parent scan_jobs metadata, or None."""
    async with get_connection() as conn:
        row = await (await conn.execute(
            "SELECT v.*, v.id AS version_id, j.title, j.rating_key, j.library_id, j.library_title, "
            "j.media_type, j.content_rating, j.year, j.show_guid, j.show_rating_key, "
            "COALESCE(j.ignored, 0) AS ignored "
            "FROM title_versions v JOIN scan_jobs j ON j.plex_guid = v.plex_guid "
            "WHERE v.id=?",
            (version_id,),
        )).fetchone()
        return dict(row) if row else None


async def get_title_versions_for_guids(guids: list[str]) -> dict[str, list[dict]]:
    """Return {plex_guid: [version_dicts]} for the given guids."""
    out: dict[str, list[dict]] = {}
    if not guids:
        return out
    async with get_connection() as conn:
        for i in range(0, len(guids), 500):
            chunk = guids[i:i + 500]
            rows = await conn.execute_fetchall(
                f"SELECT * FROM title_versions WHERE plex_guid IN ({_in(chunk)}) ORDER BY plex_guid, media_index, id",
                chunk,
            )
            for r in rows:
                out.setdefault(r["plex_guid"], []).append(dict(r))
    return out


async def get_title_versions_for_library(library_id: str) -> dict[str, list[dict]]:
    """Return {plex_guid: [version_dicts]} for every title in one library (single query)."""
    out: dict[str, list[dict]] = {}
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT v.* FROM title_versions v JOIN scan_jobs j ON j.plex_guid = v.plex_guid "
            "WHERE j.library_id=? ORDER BY v.plex_guid, v.media_index, v.id",
            (library_id,),
        )
        for r in rows:
            out.setdefault(r["plex_guid"], []).append(dict(r))
    return out


async def get_version_by_media_index(rating_key: str, media_index: int) -> dict | None:
    """Return the version row for a rating_key and Plex 0-based mediaIndex."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT v.*, v.id AS version_id, j.rating_key, j.title FROM title_versions v "
            "JOIN scan_jobs j ON j.plex_guid = v.plex_guid "
            "WHERE j.rating_key=? ORDER BY v.media_index, v.id",
            (rating_key,),
        )
    if not rows:
        return None
    for r in rows:
        if int(r["media_index"] or 0) == media_index:
            return dict(r)
    if 0 <= media_index < len(rows):
        return dict(rows[media_index])
    return None


async def sync_title_versions_batch(items: list[dict]) -> dict[str, list]:
    """Reconcile Plex media versions for existing titles without losing scan history or rehashing unchanged files.

    Each item dict carries ``plex_guid``, ``rating_key``, ``title``, and ``versions`` (each a
    dict with ``plex_media_id``, ``media_index``, ``resolution``, ``file_path``, ``part_files``,
    ``part_ids``, ``file_size``, ``duration_ms``).
    Returns ``{"inserted": [version_dicts], "removed_ids": [version_ids]}``.
    """
    if not items:
        return {"inserted": [], "removed_ids": []}
    guids = [it["plex_guid"] for it in items]
    inserted: list[dict] = []
    removed_ids: list[int] = []
    restored_pairs: list[tuple[int, str, str]] = []
    part_tuples: list[tuple[int, str, int]] = []

    async with get_connection() as conn:
        existing_by_guid: dict[str, list[dict]] = {}
        for i in range(0, len(guids), 500):
            chunk = guids[i:i + 500]
            rows = await conn.execute_fetchall(
                f"SELECT * FROM title_versions WHERE plex_guid IN ({_in(chunk)}) ORDER BY media_index, id",
                chunk,
            )
            for r in rows:
                existing_by_guid.setdefault(r["plex_guid"], []).append(dict(r))

        touched_guids: list[str] = []
        for it in items:
            guid = it["plex_guid"]
            versions = it.get("versions") or []
            if not versions:
                continue
            existing = list(existing_by_guid.get(guid, []))
            used_ids: set[int] = set()
            by_media_id = {e["plex_media_id"]: e for e in existing if e["plex_media_id"]}

            for idx, v in enumerate(versions):
                mid = str(v.get("plex_media_id") or "")
                fpath = v.get("file_path") or ""
                pfiles = v.get("part_files") or ""
                pids_raw = v.get("part_ids") or ""
                res = v.get("resolution") or ""
                fsize = int(v.get("file_size") or 0)
                dur = int(v.get("duration_ms") or 0)
                m_idx = int(v.get("media_index", idx))

                match = None
                if mid and mid in by_media_id and by_media_id[mid]["id"] not in used_ids:
                    match = by_media_id[mid]
                if match is None:
                    for e in existing:
                        if e["id"] not in used_ids and fpath and e["file_path"] == fpath:
                            match = e
                            break
                if match is None and len(existing) == 1 and len(versions) == 1 and existing[0]["id"] not in used_ids:
                    # Single-version file rename / path change on a pre-existing row.
                    if not existing[0]["plex_media_id"] or existing[0]["plex_media_id"] == mid:
                        match = existing[0]

                if match is not None:
                    vid = int(match["id"])
                    used_ids.add(vid)
                    path_changed = match["file_path"] != fpath
                    await conn.execute(
                        "UPDATE title_versions SET plex_media_id=?, media_index=?, resolution=?, file_path=?, "
                        "part_files=?, part_ids=?, file_size=?, duration_ms=?, "
                        "content_key=CASE WHEN ? THEN '' ELSE content_key END, "
                        "content_size=CASE WHEN ? THEN 0 ELSE content_size END, "
                        "content_mtime=CASE WHEN ? THEN 0 ELSE content_mtime END "
                        "WHERE id=?",
                        (mid, m_idx, res, fpath, pfiles, pids_raw, fsize, dur,
                         path_changed, path_changed, path_changed, vid),
                    )
                    if path_changed:
                        touched_guids.append(guid)
                else:
                    cur = await conn.execute(
                        "INSERT INTO title_versions(plex_guid, plex_media_id, media_index, resolution, file_path, "
                        "part_files, part_ids, file_size, duration_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                        (guid, mid, m_idx, res, fpath, pfiles, pids_raw, fsize, dur),
                    )
                    vid = int(cur.lastrowid)
                    inserted.append({
                        "id": vid,
                        "plex_guid": guid,
                        "plex_media_id": mid,
                        "media_index": m_idx,
                        "resolution": res,
                        "file_path": fpath,
                        "part_files": pfiles,
                        "part_ids": pids_raw,
                        "file_size": fsize,
                        "duration_ms": dur,
                    })
                    restored_pairs.append((vid, guid, fpath))
                    touched_guids.append(guid)

                rkey = it.get("rating_key") or ""
                if rkey:
                    for pid in _json_int_list(pids_raw):
                        part_tuples.append((pid, rkey, vid))

            unmatched = [e for e in existing if e["id"] not in used_ids]
            if unmatched:
                touched_guids.append(guid)
                for e in unmatched:
                    evid = int(e["id"])
                    removed_ids.append(evid)
                    await conn.execute(
                        "INSERT OR REPLACE INTO removed_titles(version_id, plex_guid, title, rating_key, file_path, "
                        "part_files, duration_ms, status, scan_version, finished_at, content_key, content_size, content_mtime) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (evid, guid, it.get("title", ""), it.get("rating_key", ""), e["file_path"],
                         e["part_files"] or "", int(e["duration_ms"] or 0), e["status"] or "",
                         int(e["scan_version"] or 0), e["finished_at"],
                         e["content_key"] or "", int(e["content_size"] or 0), int(e["content_mtime"] or 0)),
                    )
                await archive_version_segments(conn, [int(e["id"]) for e in unmatched])
                for e in unmatched:
                    await conn.execute("DELETE FROM title_versions WHERE id=?", (e["id"],))

        # Restore archived content keys / segments if an exact (plex_guid, file_path) version returned.
        for new_vid, guid, fpath in restored_pairs:
            if not fpath:
                continue
            rrow = await (await conn.execute(
                "SELECT version_id, content_key, content_size, content_mtime FROM removed_titles "
                "WHERE plex_guid=? AND file_path=? LIMIT 1",
                (guid, fpath),
            )).fetchone()
            if rrow:
                if rrow["content_key"]:
                    await conn.execute(
                        "UPDATE title_versions SET content_key=?, content_size=?, content_mtime=? "
                        "WHERE id=? AND COALESCE(content_key, '')=''",
                        (rrow["content_key"], rrow["content_size"], rrow["content_mtime"], new_vid),
                    )
                old_vid = rrow["version_id"]
                await conn.execute(
                    f"INSERT OR IGNORE INTO segments({_SEGMENT_COLS}) "
                    f"SELECT id, plex_guid, ?, title, start_ms, end_ms, confidence, thumbnail_path, created_at, "
                    "labels, category, severity, action, channel, source, language, detector, false_positive, "
                    "hover_status, thumbnail_ms, refined_at FROM removed_segments WHERE version_id=?",
                    (new_vid, old_vid),
                )
                await conn.execute("DELETE FROM removed_segments WHERE version_id=?", (old_vid,))
                await conn.execute("DELETE FROM removed_titles WHERE version_id=?", (old_vid,))

        if part_tuples:
            await conn.executemany(
                "INSERT OR REPLACE INTO title_parts(part_id, rating_key, version_id) VALUES(?,?,?)",
                part_tuples,
            )

        if touched_guids:
            await _refresh_job_summary_conn(conn, touched_guids)
        await conn.commit()
    return {"inserted": inserted, "removed_ids": removed_ids}


def _json_int_list(raw: str | None) -> list[int]:
    """Parse a JSON int list column, tolerating blanks and bad data."""
    try:
        value = json.loads(raw) if raw else []
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    out: list[int] = []
    for v in value:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            pass
    return out


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
            # Clear cached content key only if the file path changed so existing files are never rehashed (#235).
            " content_key=CASE WHEN file_path != ? THEN '' ELSE content_key END,"
            " content_size=CASE WHEN file_path != ? THEN 0 ELSE content_size END,"
            " content_mtime=CASE WHEN file_path != ? THEN 0 ELSE content_mtime END,"
            # Backfill show_guid/show_rating_key from Plex for episodes synced before these columns existed.
            " show_guid=CASE WHEN ? != '' THEN ? ELSE show_guid END,"
            " show_rating_key=CASE WHEN ? != '' THEN ? ELSE show_rating_key END"
            " WHERE plex_guid=?",
            [(title, file_path, part_files, rating_key, content_rating, year,
              file_path, file_path, file_path,
              show_guid, show_guid, show_rating_key, show_rating_key, plex_guid)
             for plex_guid, title, file_path, rating_key, content_rating, year, show_guid, show_rating_key, part_files in items],
        )
        # Keep single-version rows in sync when metadata is refreshed without sync_title_versions_batch.
        await conn.executemany(
            "UPDATE title_versions SET file_path=?, part_files=?, "
            "content_key=CASE WHEN file_path != ? THEN '' ELSE content_key END, "
            "content_size=CASE WHEN file_path != ? THEN 0 ELSE content_size END, "
            "content_mtime=CASE WHEN file_path != ? THEN 0 ELSE content_mtime END "
            "WHERE plex_guid=? AND (SELECT COUNT(*) FROM title_versions v2 WHERE v2.plex_guid = title_versions.plex_guid) = 1",
            [(file_path, part_files, file_path, file_path, file_path, plex_guid)
             for plex_guid, _t, file_path, _rk, _cr, _yr, _sg, _srk, part_files in items],
        )
        await conn.commit()


async def get_scan_jobs(status: str | None = None) -> list[dict]:
    """Return every version scan job joined with title metadata, newest first."""
    async with get_connection() as conn:
        base = (
            "SELECT j.*, v.id AS version_id, v.plex_media_id, v.media_index, v.resolution, "
            "v.file_path, v.part_files, v.part_ids, v.file_size, v.duration_ms, "
            "v.status, v.progress, v.force_scan, v.queue_pos, v.scan_version, "
            "v.started_at, v.finished_at, v.error_msg "
            "FROM title_versions v JOIN scan_jobs j ON j.plex_guid = v.plex_guid "
        )
        if status:
            rows = await conn.execute_fetchall(base + "WHERE v.status=? ORDER BY v.created_at DESC", (status,))
        else:
            rows = await conn.execute_fetchall(base + "ORDER BY v.created_at DESC")
        return [dict(r) for r in rows]


async def get_scan_jobs_page(statuses: list[str], limit: int, offset: int = 0) -> list[dict]:
    """Return version scan jobs whose status is in *statuses*, newest first, at most *limit* rows from *offset*."""
    if not statuses:
        return []
    marks = ",".join("?" * len(statuses))
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"SELECT j.*, v.id AS version_id, v.plex_media_id, v.media_index, v.resolution, "
            f"v.file_path, v.part_files, v.part_ids, v.file_size, v.duration_ms, "
            f"v.status, v.progress, v.force_scan, v.queue_pos, v.scan_version, "
            f"v.started_at, v.finished_at, v.error_msg "
            f"FROM title_versions v JOIN scan_jobs j ON j.plex_guid = v.plex_guid "
            f"WHERE v.status IN ({marks}) ORDER BY v.created_at DESC, v.id DESC LIMIT ? OFFSET ?",
            (*statuses, limit, offset),
        )
        return [dict(r) for r in rows]


async def count_scan_jobs_by_status() -> dict[str, int]:
    """Return {status: count} over all version scan jobs."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall("SELECT status, COUNT(*) AS n FROM title_versions GROUP BY status")
        return {(r["status"] or "unknown"): r["n"] for r in rows}


async def get_content_rating_counts() -> list[dict]:
    """Return [{content_rating, n}] for every content rating (as Plex spells it) among scan jobs."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT COALESCE(content_rating, '') AS content_rating, COUNT(*) AS n FROM scan_jobs GROUP BY 1"
        )
        return [dict(r) for r in rows]


async def get_scan_job_by_guid(plex_guid: str) -> dict | None:
    """Return one title's scan job merged with its primary version's state/content_key."""
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT * FROM scan_jobs WHERE plex_guid=?", (plex_guid,))).fetchone()
        if not row:
            return None
        d = dict(row)
        vrows = await conn.execute_fetchall(
            "SELECT * FROM title_versions WHERE plex_guid=? ORDER BY media_index, id", (plex_guid,)
        )
        if vrows:
            v0 = dict(vrows[0])
            d["version_id"] = v0["id"]
            d["content_key"] = v0.get("content_key") or d.get("content_key") or ""
            d["content_size"] = v0.get("content_size") or d.get("content_size") or 0
            d["content_mtime"] = v0.get("content_mtime") or d.get("content_mtime") or 0
            d["hayaa_sync_state"] = v0.get("hayaa_sync_state") or "pending"
            d["resolution"] = v0.get("resolution") or ""
        return d


async def get_scan_job_by_rating_key(rating_key: str) -> dict | None:
    """Return the scan job with this Plex rating key, or None (fallback when a session's GUID differs)."""
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT * FROM scan_jobs WHERE rating_key=? LIMIT 1", (rating_key,))).fetchone()
        return dict(row) if row else None


async def update_version_status(
    version_id: int,
    status: str,
    progress: float = 0.0,
    error_msg: str | None = None,
    scan_version: int | None = None,
) -> None:
    """Set one version's scan status and progress, keeping its parent scan_jobs summary synced."""
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT plex_guid FROM title_versions WHERE id=?", (version_id,))).fetchone()
        if status == "scanning":
            await conn.execute(
                "UPDATE title_versions SET status=?, progress=?, started_at=COALESCE(started_at, CURRENT_TIMESTAMP) WHERE id=?",
                (status, progress, version_id),
            )
        elif status in ("done", "failed"):
            await conn.execute(
                "UPDATE title_versions SET status=?, progress=?, finished_at=CURRENT_TIMESTAMP, error_msg=?, "
                "queue_pos=NULL, scan_version=COALESCE(?, scan_version) WHERE id=?",
                (status, progress, error_msg, scan_version if status == "done" else None, version_id),
            )
        else:
            await conn.execute(
                "UPDATE title_versions SET status=?, progress=? WHERE id=?",
                (status, progress, version_id),
            )
        if row:
            await _refresh_job_summary_conn(conn, [row["plex_guid"]])
        await conn.commit()


async def update_scan_job_status(
    plex_guid: str,
    status: str,
    progress: float = 0.0,
    error_msg: str | None = None,
    scan_version: int | None = None,
    version_id: int | None = None,
) -> None:
    """Set a job's (or specific version's) status and progress; a finished scan records the scanner revision."""
    if version_id is not None:
        await update_version_status(
            version_id, status, progress=progress, error_msg=error_msg, scan_version=scan_version
        )
        return
    async with get_connection() as conn:
        if status == "scanning":
            await conn.execute(
                "UPDATE title_versions SET status=?, progress=?, started_at=COALESCE(started_at, CURRENT_TIMESTAMP) WHERE plex_guid=?",
                (status, progress, plex_guid),
            )
            await conn.execute(
                "UPDATE scan_jobs SET status=?, progress=?, started_at=COALESCE(started_at, CURRENT_TIMESTAMP) WHERE plex_guid=?",
                (status, progress, plex_guid),
            )
        elif status in ("done", "failed"):
            sv = scan_version if status == "done" else None
            await conn.execute(
                "UPDATE title_versions SET status=?, progress=?, finished_at=CURRENT_TIMESTAMP, error_msg=?, "
                "queue_pos=NULL, scan_version=COALESCE(?, scan_version) WHERE plex_guid=?",
                (status, progress, error_msg, sv, plex_guid),
            )
            await conn.execute(
                "UPDATE scan_jobs SET status=?, progress=?, finished_at=CURRENT_TIMESTAMP, error_msg=?, "
                "queue_pos=NULL, scan_version=COALESCE(?, scan_version) WHERE plex_guid=?",
                (status, progress, error_msg, sv, plex_guid),
            )
        else:
            await conn.execute(
                "UPDATE title_versions SET status=?, progress=? WHERE plex_guid=?",
                (status, progress, plex_guid),
            )
            await conn.execute(
                "UPDATE scan_jobs SET status=?, progress=? WHERE plex_guid=?",
                (status, progress, plex_guid),
            )
        await conn.commit()


RESCAN_SCOPES = ("outdated", "with_segments", "all_scanned", "scanned_before", "whole_library", "failed")


async def get_rescan_candidates(
    scope: str,
    current_version: int,
    library_id: str | None = None,
    before: str | None = None,
) -> list[dict]:
    """Return finished, non-ignored media versions matching a rescan scope."""
    if scope not in RESCAN_SCOPES:
        raise ValueError(f"unknown rescan scope {scope!r}")
    if scope == "whole_library" and not library_id:
        raise ValueError("whole_library needs a library")
    status_sql = {"whole_library": "v.status IN ('done', 'failed')", "failed": "v.status = 'failed'"}.get(
        scope, "v.status = 'done'"
    )
    where = [status_sql, "COALESCE(j.ignored, 0) = 0"]
    params: list = []
    if scope == "outdated":
        where.append("COALESCE(v.scan_version, 0) < ?")
        params.append(current_version)
    elif scope == "with_segments":
        where.append(
            f"EXISTS (SELECT 1 FROM segments s WHERE (s.version_id = v.id OR (COALESCE(s.version_id, 0) = 0 AND s.plex_guid = v.plex_guid)) AND {_ACTIVE_S})"
        )
    elif scope == "scanned_before":
        if not before:
            raise ValueError("scanned_before needs a date")
        where.append("v.finished_at < ?")
        params.append(before)
    if library_id:
        where.append("j.library_id = ?")
        params.append(library_id)
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"SELECT v.id AS version_id, v.plex_guid, j.library_id, j.content_rating, j.ignored, "
            f"v.error_msg, v.file_path, v.part_files, v.resolution "
            f"FROM title_versions v JOIN scan_jobs j ON j.plex_guid = v.plex_guid WHERE {' AND '.join(where)}",
            params,
        )
        return [dict(r) for r in rows]


def _like_literal(text: str) -> str:
    """Escape LIKE wildcards so a title such as "100% Wolf" matches literally (ESCAPE '\\')."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def count_failed_ignored(library_id: str | None = None) -> int:
    """Return how many ignored versions are still marked failed (they are never scanned, so it is noise)."""
    sql = ("SELECT COUNT(*) FROM title_versions v JOIN scan_jobs j ON j.plex_guid = v.plex_guid "
           "WHERE v.status='failed' AND COALESCE(j.ignored, 0) = 1")
    params: list = []
    if library_id:
        sql, params = sql + " AND j.library_id = ?", [library_id]
    async with get_connection() as conn:
        row = await (await conn.execute(sql, params)).fetchone()
        return row[0]


async def clear_failed_ignored(library_id: str | None = None) -> int:
    """Reset ignored versions from failed to pending, dropping their stale error; return how many."""
    where = "WHERE status='failed' AND plex_guid IN (SELECT plex_guid FROM scan_jobs WHERE COALESCE(ignored, 0) = 1"
    params: list = []
    if library_id:
        where += " AND library_id = ?"
        params.append(library_id)
    where += ")"
    async with get_connection() as conn:
        guids = [r[0] for r in await conn.execute_fetchall(
            f"SELECT DISTINCT plex_guid FROM title_versions {where}", params
        )]
        cursor = await conn.execute(
            f"UPDATE title_versions SET status='pending', progress=0, started_at=NULL, finished_at=NULL, error_msg=NULL {where}",
            params,
        )
        if guids:
            await _refresh_job_summary_conn(conn, guids)
        await conn.commit()
        return cursor.rowcount


async def get_show_episode_jobs(
    show_guid: str = "", show_title: str = "", library_id: str | None = None, season: str = ""
) -> list[dict]:
    """Return a show's non-ignored episode versions, by show GUID or, failing that, by "<show> – " title."""
    season_like = f"{_like_literal(show_title)} – {_like_literal(season)} – %" if season and show_title else ""
    async with get_connection() as conn:
        rows: list = []
        if show_guid:
            sql = ("SELECT v.id AS version_id, v.plex_guid, j.library_id, j.content_rating, j.ignored, v.status "
                   "FROM title_versions v JOIN scan_jobs j ON j.plex_guid = v.plex_guid "
                   "WHERE j.show_guid = ? AND COALESCE(j.ignored, 0) = 0")
            params = [show_guid]
            if season_like:
                sql += " AND j.title LIKE ? ESCAPE '\\'"
                params.append(season_like)
            rows = await conn.execute_fetchall(sql, params)
        if not rows and show_title:
            sql = ("SELECT v.id AS version_id, v.plex_guid, j.library_id, j.content_rating, j.ignored, v.status "
                   "FROM title_versions v JOIN scan_jobs j ON j.plex_guid = v.plex_guid "
                   "WHERE j.media_type = 'episode' AND j.title LIKE ? ESCAPE '\\' AND COALESCE(j.ignored, 0) = 0")
            params: list = [season_like or f"{_like_literal(show_title)} – %"]
            if library_id:
                sql += " AND j.library_id = ?"
                params.append(library_id)
            rows = await conn.execute_fetchall(sql, params)
        return [dict(r) for r in rows]


async def update_part_files(plex_guid: str, part_files: str, version_id: int | None = None) -> None:
    """Update the part_files JSON for an existing version/job without touching status or progress."""
    async with get_connection() as conn:
        if version_id is not None:
            await conn.execute("UPDATE title_versions SET part_files=? WHERE id=?", (part_files, version_id))
        else:
            await conn.execute("UPDATE scan_jobs SET part_files=? WHERE plex_guid=?", (part_files, plex_guid))
            await conn.execute("UPDATE title_versions SET part_files=? WHERE plex_guid=?", (part_files, plex_guid))
        await conn.commit()


async def reset_version_job(version_id: int) -> None:
    """Reset one media version's scan job back to pending."""
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT plex_guid FROM title_versions WHERE id=?", (version_id,))).fetchone()
        await conn.execute(
            "UPDATE title_versions SET status='pending', progress=0, started_at=NULL, finished_at=NULL, "
            "error_msg=NULL, queue_pos=NULL WHERE id=?",
            (version_id,),
        )
        if row:
            await _refresh_job_summary_conn(conn, [row["plex_guid"]])
        await conn.commit()


async def reset_version_jobs(version_ids: list[int]) -> int:
    """Mark many version jobs pending (in chunks); returns how many matched."""
    total = 0
    guids: set[str] = set()
    async with get_connection() as conn:
        for i in range(0, len(version_ids), 500):
            chunk = version_ids[i:i + 500]
            grows = await conn.execute_fetchall(
                f"SELECT DISTINCT plex_guid FROM title_versions WHERE id IN ({_in(chunk)})", chunk
            )
            guids.update(r["plex_guid"] for r in grows)
            cursor = await conn.execute(
                "UPDATE title_versions SET status='pending', progress=0, started_at=NULL, finished_at=NULL, "
                f"error_msg=NULL, queue_pos=NULL WHERE id IN ({_in(chunk)})",
                chunk,
            )
            total += cursor.rowcount
        if guids:
            await _refresh_job_summary_conn(conn, list(guids))
        await conn.commit()
    return total


async def reset_scan_job(plex_guid: str) -> None:
    """Reset every version of a title back to pending."""
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE title_versions SET status='pending', progress=0, started_at=NULL, finished_at=NULL, error_msg=NULL, "
            "queue_pos=NULL WHERE plex_guid=?",
            (plex_guid,),
        )
        await conn.execute(
            "UPDATE scan_jobs SET status='pending', progress=0, started_at=NULL, finished_at=NULL, error_msg=NULL, "
            "queue_pos=NULL WHERE plex_guid=?",
            (plex_guid,),
        )
        await conn.commit()


async def reset_scan_jobs(plex_guids: list[str]) -> int:
    """Mark every version of many titles pending (in chunks, within SQLite's variable limit); returns matched count."""
    total = 0
    async with get_connection() as conn:
        for i in range(0, len(plex_guids), 500):
            chunk = plex_guids[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            cursor = await conn.execute(
                "UPDATE title_versions SET status='pending', progress=0, started_at=NULL, finished_at=NULL, "
                f"error_msg=NULL, queue_pos=NULL WHERE plex_guid IN ({placeholders})",
                chunk,
            )
            await conn.execute(
                "UPDATE scan_jobs SET status='pending', progress=0, started_at=NULL, finished_at=NULL, "
                f"error_msg=NULL, queue_pos=NULL WHERE plex_guid IN ({placeholders})",
                chunk,
            )
            total += cursor.rowcount
        await conn.commit()
    return total


async def reset_interrupted_scans() -> int:
    """Move versions/jobs left in 'scanning' back to 'pending'; returns how many versions were reset."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE title_versions SET status='pending', started_at=NULL WHERE status='scanning'"
        )
        await conn.execute(
            "UPDATE scan_jobs SET status='pending', started_at=NULL WHERE status='scanning'"
        )
        await conn.commit()
        return cursor.rowcount


async def set_force_scan(plex_guid: str, force: bool, version_id: int | None = None) -> None:
    """Set or unset the force_scan flag for one version or all versions of a title."""
    val = 1 if force else 0
    async with get_connection() as conn:
        if version_id is not None:
            await conn.execute("UPDATE title_versions SET force_scan=? WHERE id=?", (val, version_id))
            await _refresh_job_summary_conn(conn, [plex_guid])
        else:
            await conn.execute("UPDATE title_versions SET force_scan=? WHERE plex_guid=?", (val, plex_guid))
            await conn.execute("UPDATE scan_jobs SET force_scan=? WHERE plex_guid=?", (val, plex_guid))
        await conn.commit()


async def set_ignored(plex_guid: str, ignored: bool) -> None:
    """Set or unset the ignored flag for a specific title."""
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE scan_jobs SET ignored=? WHERE plex_guid=?",
            (1 if ignored else 0, plex_guid),
        )
        if ignored:
            # An ignored title leaves the queue unless it was force-scanned on purpose.
            await conn.execute(
                "UPDATE title_versions SET queue_pos=NULL WHERE plex_guid=? AND status='pending' "
                "AND COALESCE(force_scan,0)=0",
                (plex_guid,),
            )
            await conn.execute(
                "UPDATE scan_jobs SET queue_pos=NULL WHERE plex_guid=? AND status='pending' "
                "AND COALESCE(force_scan,0)=0",
                (plex_guid,),
            )
        await conn.commit()


# ── Scan queue (#174, #233) ───────────────────────────────────────────────────
# Queue membership and order live in title_versions.queue_pos: a pending version with a position is
# queued, lower runs first, force_scan versions run before the rest. Workers claim per version
# with claim_next_scan_job so multi-version titles scan independently without double claiming.

_QUEUE_ORDER = "COALESCE(v.force_scan,0) DESC, v.queue_pos, v.id"
_QUEUE_SELECT_FROM = (
    "SELECT v.*, v.id AS version_id, j.title, j.rating_key, j.library_id, j.library_title, "
    "j.media_type, j.content_rating, j.year, j.show_guid, j.show_rating_key, "
    "COALESCE(j.ignored, 0) AS ignored "
    "FROM title_versions v JOIN scan_jobs j ON j.plex_guid = v.plex_guid"
)
# A forced version may belong to an ignored title; the user asked for that scan explicitly.
_CLAIMABLE_WHERE = (
    "v.status='pending' AND v.queue_pos IS NOT NULL "
    "AND (COALESCE(j.ignored,0)=0 OR COALESCE(v.force_scan,0)=1 OR COALESCE(j.force_scan,0)=1)"
)


async def claim_next_scan_job(force_only: bool = False) -> dict | None:
    """Atomically take the next queued version: mark it 'scanning' and return its joined row, or None."""
    extra = " AND (COALESCE(v.force_scan,0)=1 OR COALESCE(j.force_scan,0)=1)" if force_only else ""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE title_versions SET status='scanning', started_at=COALESCE(started_at, CURRENT_TIMESTAMP) "
            "WHERE status='pending' AND id=("
            f"SELECT v.id FROM title_versions v JOIN scan_jobs j ON j.plex_guid = v.plex_guid "
            f"WHERE {_CLAIMABLE_WHERE}{extra} ORDER BY {_QUEUE_ORDER} LIMIT 1"
            ") RETURNING id"
        )
        claimed = await cursor.fetchone()
        if not claimed:
            await conn.commit()
            return None
        vid = int(claimed["id"])
        row = await (await conn.execute(f"{_QUEUE_SELECT_FROM} WHERE v.id=?", (vid,))).fetchone()
        if row:
            await _refresh_job_summary_conn(conn, [row["plex_guid"]])
        await conn.commit()
        return dict(row) if row else None


async def queue_version_jobs(version_ids: list[int], *, to_end: bool = False) -> int:
    """Append pending version jobs to the queue in the order given; return how many were queued."""
    if not version_ids:
        return 0
    queued = 0
    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        row = await (await conn.execute("SELECT COALESCE(MAX(queue_pos), 0) AS m FROM title_versions")).fetchone()
        pos = row["m"]
        cond = "" if to_end else " AND queue_pos IS NULL"
        for vid in version_ids:
            cursor = await conn.execute(
                f"UPDATE title_versions SET queue_pos=? WHERE id=? AND status='pending'{cond}",
                (pos + 1, vid),
            )
            if cursor.rowcount:
                pos += 1
                queued += cursor.rowcount
        await conn.commit()
    return queued


async def queue_scan_jobs(plex_guids: list[str], *, to_end: bool = False) -> int:
    """Append pending versions for the given guids to the queue; returns how many versions were queued."""
    if not plex_guids:
        return 0
    queued = 0
    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        row = await (await conn.execute("SELECT COALESCE(MAX(queue_pos), 0) AS m FROM title_versions")).fetchone()
        pos = row["m"]
        cond = "" if to_end else " AND queue_pos IS NULL"
        for guid in plex_guids:
            vrows = await conn.execute_fetchall(
                f"SELECT id FROM title_versions WHERE plex_guid=? AND status='pending'{cond} ORDER BY media_index, id",
                (guid,),
            )
            for vr in vrows:
                await conn.execute("UPDATE title_versions SET queue_pos=? WHERE id=?", (pos + 1, vr["id"]))
                pos += 1
                queued += 1
        await conn.commit()
    return queued


async def set_version_queue_order(version_ids: list[int]) -> None:
    """Rebuild the queue: every pending version leaves it, then *version_ids* take positions 1..N."""
    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute("UPDATE title_versions SET queue_pos=NULL WHERE status='pending'")
        await conn.executemany(
            "UPDATE title_versions SET queue_pos=? WHERE id=? AND status='pending'",
            [(i + 1, vid) for i, vid in enumerate(version_ids)],
        )
        await conn.commit()


async def set_scan_queue_order(plex_guids: list[str]) -> None:
    """Rebuild the queue in the given guid order (versions within a title ordered by media_index, id)."""
    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute("UPDATE title_versions SET queue_pos=NULL WHERE status='pending'")
        pos = 1
        for guid in plex_guids:
            vrows = await conn.execute_fetchall(
                "SELECT id FROM title_versions WHERE plex_guid=? AND status='pending' ORDER BY media_index, id",
                (guid,),
            )
            for vr in vrows:
                await conn.execute("UPDATE title_versions SET queue_pos=? WHERE id=?", (pos, vr["id"]))
                pos += 1
        await conn.commit()


async def dequeue_version_jobs(version_ids: list[int]) -> None:
    """Take version jobs out of the queue without changing their status."""
    if not version_ids:
        return
    async with get_connection() as conn:
        for i in range(0, len(version_ids), 500):
            chunk = version_ids[i:i + 500]
            await conn.execute(f"UPDATE title_versions SET queue_pos=NULL WHERE id IN ({_in(chunk)})", chunk)
        await conn.commit()


async def dequeue_scan_jobs(plex_guids: list[str]) -> None:
    """Take all versions of the given guids out of the queue without changing their status."""
    if not plex_guids:
        return
    async with get_connection() as conn:
        for i in range(0, len(plex_guids), 500):
            chunk = plex_guids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            await conn.execute(f"UPDATE title_versions SET queue_pos=NULL WHERE plex_guid IN ({marks})", chunk)
        await conn.commit()


async def release_version_job(version_id: int, *, requeue: bool, progress: float = 0.0) -> None:
    """Give a claimed version back as pending: behind the rest of the queue (requeue) or out of it."""
    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        vrow = await (await conn.execute("SELECT plex_guid FROM title_versions WHERE id=?", (version_id,))).fetchone()
        if requeue:
            row = await (await conn.execute("SELECT COALESCE(MAX(queue_pos), 0) AS m FROM title_versions")).fetchone()
            await conn.execute(
                "UPDATE title_versions SET status='pending', progress=?, queue_pos=? WHERE id=?",
                (progress, row["m"] + 1, version_id),
            )
        else:
            await conn.execute(
                "UPDATE title_versions SET status='pending', progress=?, queue_pos=NULL WHERE id=?",
                (progress, version_id),
            )
        if vrow:
            await _refresh_job_summary_conn(conn, [vrow["plex_guid"]])
        await conn.commit()


async def release_scan_job(
    plex_guid: str, *, requeue: bool, progress: float = 0.0, version_id: int | None = None
) -> None:
    """Give a claimed job (or version) back as pending."""
    if version_id is not None:
        await release_version_job(version_id, requeue=requeue, progress=progress)
        return
    async with get_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        if requeue:
            row = await (await conn.execute("SELECT COALESCE(MAX(queue_pos), 0) AS m FROM title_versions")).fetchone()
            await conn.execute(
                "UPDATE title_versions SET status='pending', progress=?, queue_pos=? WHERE plex_guid=?",
                (progress, row["m"] + 1, plex_guid),
            )
        else:
            await conn.execute(
                "UPDATE title_versions SET status='pending', progress=?, queue_pos=NULL WHERE plex_guid=?",
                (progress, plex_guid),
            )
        await _refresh_job_summary_conn(conn, [plex_guid])
        await conn.commit()


async def get_queued_scan_jobs(limit: int | None = None, offset: int = 0) -> list[dict]:
    """Return the queued (pending, positioned) version jobs in execution order: force first, then position."""
    sql = f"{_QUEUE_SELECT_FROM} WHERE v.status='pending' AND v.queue_pos IS NOT NULL ORDER BY {_QUEUE_ORDER}"
    params: tuple = ()
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params = (limit, offset)
    async with get_connection() as conn:
        return [dict(r) for r in await conn.execute_fetchall(sql, params)]


async def count_queued_scan_jobs() -> int:
    """Return how many version jobs are waiting in the queue."""
    async with get_connection() as conn:
        row = await (await conn.execute(
            "SELECT COUNT(*) FROM title_versions WHERE status='pending' AND queue_pos IS NOT NULL"
        )).fetchone()
        return row[0]


async def get_unqueued_pending_scan_jobs() -> list[dict]:
    """Return pending version jobs that are not in the queue."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"{_QUEUE_SELECT_FROM} WHERE v.status='pending' AND v.queue_pos IS NULL ORDER BY v.id"
        )
        return [dict(r) for r in rows]


async def get_scanning_guids() -> list[str]:
    """Return distinct guids with a version currently being scanned, sorted."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT DISTINCT plex_guid FROM title_versions WHERE status='scanning' ORDER BY plex_guid"
        )
        return [r["plex_guid"] for r in rows]


async def get_scanning_version_ids() -> list[int]:
    """Return version_ids currently being scanned, sorted."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT id FROM title_versions WHERE status='scanning' ORDER BY id"
        )
        return [int(r["id"]) for r in rows]


async def get_scan_jobs_by_guids(guids: list[str]) -> dict[str, dict]:
    """Return {plex_guid: job} for the provided guids, queried in chunks of 500 (SQLite variable limit)."""
    out: dict[str, dict] = {}
    if not guids:
        return out
    async with get_connection() as conn:
        for i in range(0, len(guids), 500):
            chunk = guids[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = await conn.execute_fetchall(
                f"SELECT * FROM scan_jobs WHERE plex_guid IN ({placeholders})", chunk
            )
            out.update({row["plex_guid"]: dict(row) for row in rows})
    return out


async def save_proxy_session(session_id: str, data: dict) -> None:
    """Insert or update a proxy session: rating_key, version_id, username, client_id, client_title, offset_ms, ranges, spans, removed."""
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO proxy_sessions(session_id, rating_key, version_id, username, client_id, client_title, offset_ms, "
            "ranges, spans, removed, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(session_id) DO UPDATE SET spans=excluded.spans, removed=excluded.removed, "
            "version_id=CASE WHEN excluded.version_id > 0 THEN excluded.version_id ELSE proxy_sessions.version_id END, "
            "updated_at=CURRENT_TIMESTAMP",
            (session_id, data["rating_key"], int(data.get("version_id") or 0), data.get("username") or "",
             data.get("client_id", ""), data.get("client_title", ""), data.get("offset_ms", 0),
             json.dumps(data.get("ranges", [])), json.dumps(data.get("spans", [])), json.dumps(data.get("removed", []))),
        )
        await conn.commit()


async def get_proxy_session(session_id: str) -> dict | None:
    """Return a stored proxy session with ranges/spans/removed decoded, or None."""
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT * FROM proxy_sessions WHERE session_id=?", (session_id,))).fetchone()
    if not row:
        return None
    d = dict(row)
    for key in ("ranges", "spans", "removed"):
        d[key] = [tuple(x) if isinstance(x, list) else x for x in json.loads(d[key] or "[]")]
    return d


async def is_proxied(client_id: str, rating_key: str, within_hours: int = 12) -> bool:
    """True when the proxy edited a stream of this title for this client recently."""
    async with get_connection() as conn:
        row = await (await conn.execute(
            "SELECT 1 FROM proxy_sessions WHERE client_id=? AND rating_key=? AND updated_at > datetime('now', ?) LIMIT 1",
            (client_id, rating_key, f"-{int(within_hours)} hours"),
        )).fetchone()
    return row is not None


async def count_recent_proxy_sessions(within_hours: int = 24) -> int:
    """How many streams the proxy edited in the last *within_hours* hours."""
    async with get_connection() as conn:
        row = await (await conn.execute(
            "SELECT COUNT(*) FROM proxy_sessions WHERE updated_at > datetime('now', ?)", (f"-{int(within_hours)} hours",)
        )).fetchone()
    return row[0]


async def prune_proxy_sessions(older_than_days: int = 2) -> int:
    """Delete proxy sessions not touched for the given number of days; return how many."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "DELETE FROM proxy_sessions WHERE updated_at < datetime('now', ?)", (f"-{int(older_than_days)} days",)
        )
        await conn.commit()
        return cur.rowcount


async def get_keyframes(file_path: str, size: int, mtime: int) -> list[int] | None:
    """Return cached keyframe times (ms) for a file, or None when missing or the file changed."""
    async with get_connection() as conn:
        row = await (await conn.execute(
            "SELECT keyframes FROM keyframe_index WHERE file_path=? AND size=? AND mtime=?", (file_path, size, mtime)
        )).fetchone()
    return json.loads(row[0]) if row else None


async def put_keyframes(file_path: str, size: int, mtime: int, keyframes: list[int]) -> None:
    """Store a file's keyframe times (ms), replacing an older index of the same path."""
    async with get_connection() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO keyframe_index(file_path, size, mtime, keyframes) VALUES(?,?,?,?)",
            (file_path, size, mtime, json.dumps(keyframes)),
        )
        await conn.commit()


async def get_file_content_key(file_path: str, size: int, mtime: int) -> str | None:
    """Return the cached Hayaa content key for a file on title_versions, or None when missing or changed."""
    async with get_connection() as conn:
        row = await (await conn.execute(
            "SELECT content_key FROM title_versions "
            "WHERE file_path=? AND content_size=? AND content_mtime=? AND COALESCE(content_key, '') != '' LIMIT 1",
            (file_path, size, mtime),
        )).fetchone()
    return row[0] if row else None


async def set_title_content_key(
    plex_guid: str, content_key: str, size: int, mtime: int = 0, version_id: int | None = None
) -> None:
    """Keep a version's content key on its row (#231, #233, #235): computed once, never read from disk again."""
    async with get_connection() as conn:
        if version_id is not None:
            await conn.execute(
                "UPDATE title_versions SET content_key=?, content_size=?, content_mtime=? WHERE id=?",
                (content_key, size, mtime, version_id),
            )
        else:
            await conn.execute(
                "UPDATE title_versions SET content_key=?, content_size=?, content_mtime=? WHERE plex_guid=?",
                (content_key, size, mtime, plex_guid),
            )
        await conn.commit()


async def mark_hayaa_sending(targets: list[int] | list[str]) -> None:
    """Versions (or titles) about to be pushed; a change while the push runs sets them back to pending."""
    if not targets:
        return
    col = "id" if isinstance(targets[0], int) else "plex_guid"
    async with get_connection() as conn:
        for i in range(0, len(targets), 500):
            chunk = targets[i:i + 500]
            await conn.execute(f"UPDATE title_versions SET hayaa_sync_state='sending' WHERE {col} IN ({_in(chunk)})", chunk)
        await conn.commit()


async def mark_hayaa_synced(targets: list[int] | list[str]) -> None:
    """Hayaa accepted everything for these versions (or titles); only those not changed since are marked."""
    if not targets:
        return
    col = "id" if isinstance(targets[0], int) else "plex_guid"
    async with get_connection() as conn:
        for i in range(0, len(targets), 500):
            chunk = targets[i:i + 500]
            await conn.execute(
                f"UPDATE title_versions SET hayaa_sync_state='synced' WHERE hayaa_sync_state='sending' AND {col} IN ({_in(chunk)})",
                chunk,
            )
        await conn.commit()


async def count_hayaa_pending() -> int:
    """Single-file versions with something not yet accepted by Hayaa."""
    async with get_connection() as conn:
        row = await (await conn.execute(
            "SELECT COUNT(*) FROM title_versions v JOIN scan_jobs j ON j.plex_guid = v.plex_guid "
            "WHERE COALESCE(v.hayaa_sync_state, 'pending') != 'synced' "
            "AND v.status = 'done' AND COALESCE(v.file_path, '') != '' AND COALESCE(j.ignored, 0) = 0 "
            "AND COALESCE(json_array_length(NULLIF(v.part_files, '')), 0) <= 1"
        )).fetchone()
    return int(row[0])


async def mark_all_hayaa_pending() -> None:
    """Force sync: every version is sent again (Hayaa ignores what it already has)."""
    async with get_connection() as conn:
        await conn.execute("UPDATE title_versions SET hayaa_sync_state='pending'")
        await conn.commit()


# Progress and pacing a Hayaa server keeps track of with this instance; meaningless to another server.
HAYAA_SERVER_SETTINGS = (
    "hayaa_feed_cursor", "hayaa_partial", "hayaa_deferred", "hayaa_quota",
    "hayaa_paused_until", "hayaa_last_result", "hayaa_last_sync",
)


async def reset_hayaa_state() -> None:
    """Forget everything exchanged with a Hayaa server (#237), so a new server gets it all again.

    Clears the sent-ledger, staged claims, the title map and the overrides (keyed by that
    server's claim ids), drops HAYAA_SERVER_SETTINGS and marks every version pending. Content
    keys (they describe the files) and segments are kept.
    """
    async with get_connection() as conn:
        for table in ("hayaa_sent", "hayaa_claims", "hayaa_titles", "hayaa_overrides"):
            await conn.execute(f"DELETE FROM {table}")
        await conn.execute(
            f"DELETE FROM settings WHERE key IN ({_in(HAYAA_SERVER_SETTINGS)})", HAYAA_SERVER_SETTINGS
        )
        await conn.execute("UPDATE title_versions SET hayaa_sync_state='pending'")
        await conn.commit()


async def set_file_content_key(
    file_path: str, size: int, mtime: int, content_key: str, version_id: int | None = None
) -> None:
    """Store a file's Hayaa content key on matching title_versions rows (#233, #235)."""
    async with get_connection() as conn:
        if version_id is not None:
            await conn.execute(
                "UPDATE title_versions SET content_key=?, content_size=?, content_mtime=? WHERE id=?",
                (content_key, size, mtime, version_id),
            )
        else:
            await conn.execute(
                "UPDATE title_versions SET content_key=?, content_size=?, content_mtime=? WHERE file_path=?",
                (content_key, size, mtime, file_path),
            )
        await conn.commit()


async def get_flagged_files_without_keyframes(limit: int) -> list[str]:
    """Return single-file media paths of versions with active skip segments and no keyframe index yet."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            f"""
            SELECT DISTINCT v.file_path FROM title_versions v
            JOIN segments s ON (s.version_id = v.id OR (COALESCE(s.version_id, 0) = 0 AND s.plex_guid = v.plex_guid))
            LEFT JOIN keyframe_index k ON k.file_path = v.file_path
            WHERE {_ACTIVE_S} AND COALESCE(v.file_path,'') != ''
              AND COALESCE(json_array_length(NULLIF(v.part_files, '')), 0) <= 1
              AND k.file_path IS NULL
            LIMIT ?
            """,
            (limit,),
        )
    return [r[0] for r in rows]


async def get_file_for_rating_key(rating_key: str, version_id: int | None = None) -> str | None:
    """Return the single media file of a version (or title's first version), or None for unknown or multi-part."""
    async with get_connection() as conn:
        if version_id is not None:
            row = await (await conn.execute(
                "SELECT file_path, part_files FROM title_versions WHERE id=?", (version_id,)
            )).fetchone()
        else:
            row = await (await conn.execute(
                "SELECT v.file_path, v.part_files FROM title_versions v "
                "JOIN scan_jobs j ON j.plex_guid = v.plex_guid "
                "WHERE j.rating_key=? ORDER BY v.media_index, v.id LIMIT 1",
                (rating_key,),
            )).fetchone()
            if not row:
                row = await (await conn.execute(
                    "SELECT file_path, part_files FROM scan_jobs WHERE rating_key=? LIMIT 1", (rating_key,)
                )).fetchone()
    if not row or not row[0]:
        return None
    parts = json.loads(row[1]) if row[1] else []
    return row[0] if len(parts) <= 1 else None


async def replace_title_parts(pairs: list[tuple[int, str]] | list[tuple[int, str, int]]) -> None:
    """Store (part_id, rating_key[, version_id]) tuples, replacing any earlier owner of a part id."""
    if not pairs:
        return
    norm = [(p[0], p[1], p[2] if len(p) > 2 else 0) for p in pairs]
    async with get_connection() as conn:
        await conn.executemany(
            "INSERT OR REPLACE INTO title_parts(part_id, rating_key, version_id) VALUES(?,?,?)", norm
        )
        await conn.commit()


async def count_parts_for_rating_key(rating_key: str) -> int:
    """How many Plex parts (files, over all versions) a title has, from the library pass."""
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT COUNT(*) FROM title_parts WHERE rating_key=?", (rating_key,))).fetchone()
    return row[0]


async def count_parts_for_version(version_id: int) -> int:
    """How many Plex parts one media version has."""
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT COUNT(*) FROM title_parts WHERE version_id=?", (version_id,))).fetchone()
        if row and row[0] > 0:
            return row[0]
        vrow = await (await conn.execute("SELECT part_files FROM title_versions WHERE id=?", (version_id,))).fetchone()
    if not vrow:
        return 0
    parts = _json_list(vrow["part_files"])
    return max(len(parts), 1)


async def get_rating_key_for_part(part_id: int) -> str | None:
    """Return the rating key a Plex part id belongs to, or None when unknown."""
    async with get_connection() as conn:
        row = await (await conn.execute("SELECT rating_key FROM title_parts WHERE part_id=?", (part_id,))).fetchone()
        return row[0] if row else None


async def get_part_info(part_id: int) -> tuple[str, int] | None:
    """Return (rating_key, version_id) for a Plex part id, or None when unknown."""
    async with get_connection() as conn:
        row = await (await conn.execute(
            "SELECT rating_key, COALESCE(version_id, 0) FROM title_parts WHERE part_id=?", (part_id,)
        )).fetchone()
        return (row[0], int(row[1])) if row else None


async def get_cast_keys_to_sync(limit: int, refresh_days: int = 30) -> list[str]:
    """Return up to *limit* Plex rating keys (movies, and shows for episodes) never synced or synced over refresh_days ago."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT k.rating_key FROM (
                SELECT DISTINCT CASE WHEN media_type='episode' AND COALESCE(show_rating_key,'') != ''
                                     THEN show_rating_key ELSE rating_key END AS rating_key
                FROM scan_jobs WHERE COALESCE(rating_key,'') != ''
            ) k
            LEFT JOIN cast_synced c ON c.rating_key = k.rating_key
            WHERE c.rating_key IS NULL OR c.synced_at < datetime('now', ?)
            ORDER BY c.synced_at IS NOT NULL, c.synced_at LIMIT ?
            """,
            (f"-{int(refresh_days)} days", limit),
        )
        return [r[0] for r in rows]


async def replace_cast(cast_by_key: dict[str, list[str]]) -> None:
    """Replace the stored cast of each rating key and mark it synced (an empty list is a valid result)."""
    if not cast_by_key:
        return
    async with get_connection() as conn:
        keys = list(cast_by_key)
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            await conn.execute(f"DELETE FROM title_cast WHERE rating_key IN ({','.join('?' * len(chunk))})", chunk)
        await conn.executemany(
            "INSERT OR IGNORE INTO title_cast(rating_key, actor) VALUES(?,?)",
            [(k, a) for k, actors in cast_by_key.items() for a in actors],
        )
        await conn.executemany(
            "INSERT INTO cast_synced(rating_key, synced_at) VALUES(?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(rating_key) DO UPDATE SET synced_at=CURRENT_TIMESTAMP",
            [(k,) for k in keys],
        )
        await conn.commit()


async def put_actor_photos(photos: dict[str, str]) -> None:
    """Store or replace each actor's photo URL ({actor: thumb}); empty input is a no-op."""
    if not photos:
        return
    async with get_connection() as conn:
        await conn.executemany(
            "INSERT INTO actor_photos(actor, thumb) VALUES(?,?) ON CONFLICT(actor) DO UPDATE SET thumb=excluded.thumb",
            list(photos.items()),
        )
        await conn.commit()


async def get_actor_photos_for_library(library_id: str) -> dict[str, str]:
    """Return {actor: photo URL} for every actor in a library's titles that has one (single query)."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT DISTINCT p.actor, p.thumb FROM scan_jobs j
            JOIN title_cast c ON c.rating_key =
                CASE WHEN j.media_type='episode' AND COALESCE(j.show_rating_key,'') != ''
                     THEN j.show_rating_key ELSE j.rating_key END
            JOIN actor_photos p ON p.actor = c.actor
            WHERE j.library_id = ?
            """,
            (library_id,),
        )
    return {r[0]: r[1] for r in rows}


async def reset_cast_synced() -> None:
    """Forget which titles' cast was synced, so the cast job fetches every title again."""
    async with get_connection() as conn:
        await conn.execute("DELETE FROM cast_synced")
        await conn.commit()


async def get_cast_for_library(library_id: str) -> dict[str, list[str]]:
    """Return {plex_guid: [actor, ...]} for a library's titles in one query; episodes get their show's cast."""
    out: dict[str, list[str]] = {}
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT j.plex_guid, c.actor FROM scan_jobs j
            JOIN title_cast c ON c.rating_key =
                CASE WHEN j.media_type='episode' AND COALESCE(j.show_rating_key,'') != ''
                     THEN j.show_rating_key ELSE j.rating_key END
            WHERE j.library_id = ? ORDER BY c.actor
            """,
            (library_id,),
        )
    for r in rows:
        out.setdefault(r[0], []).append(r[1])
    return out


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



async def get_segment_counts_by_version_for_library(library_id: str) -> dict[int, int]:
    """Return {version_id: active_segment_count} for all versions in a library (single query)."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT s.version_id, COUNT(*) AS cnt
            FROM segments s
            JOIN scan_jobs j ON j.plex_guid = s.plex_guid
            WHERE j.library_id = ? AND COALESCE(s.false_positive, 0) = 0 AND s.version_id > 0
            GROUP BY s.version_id
            """,
            (library_id,),
        )
        return {int(row["version_id"]): row["cnt"] for row in rows}


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
            WHERE j.library_id = ? AND COALESCE(s.false_positive, 0) IN (1, 2)
            GROUP BY s.plex_guid
            """,
            (library_id,),
        )
        return {row["plex_guid"]: row["cnt"] for row in rows}


async def get_false_positive_counts_by_version_for_library(library_id: str) -> dict[int, int]:
    """Return {version_id: fp_count} for all versions in a library (single query)."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT s.version_id, COUNT(*) AS cnt
            FROM segments s
            JOIN scan_jobs j ON j.plex_guid = s.plex_guid
            WHERE j.library_id = ? AND COALESCE(s.false_positive, 0) IN (1, 2) AND s.version_id > 0
            GROUP BY s.version_id
            """,
            (library_id,),
        )
        return {int(row["version_id"]): row["cnt"] for row in rows}

# ── Segment Library Sharing ────────────────────────────────────────────────────

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
    """Return all locally-scanned single-file/primary versions with their segments."""
    async with get_connection() as conn:
        seg_rows = await conn.execute_fetchall(
            """
            SELECT s.*, v.file_path, j.title AS job_title
            FROM segments s
            JOIN title_versions v ON v.id = s.version_id
            JOIN scan_jobs j ON j.plex_guid = v.plex_guid
            WHERE v.status = 'done' AND v.file_path != '' AND COALESCE(s.false_positive, 0) = 0
            ORDER BY j.title, s.start_ms
            """
        )
        job_rows = await conn.execute_fetchall(
            """
            SELECT v.id AS version_id, j.plex_guid, j.title, v.file_path
            FROM title_versions v
            JOIN scan_jobs j ON j.plex_guid = v.plex_guid
            WHERE v.status = 'done' AND v.file_path != ''
            ORDER BY j.title, v.media_index, v.id
            """
        )

    job_map: dict[int, dict] = {
        int(r["version_id"]): {
            "version_id": int(r["version_id"]),
            "plex_guid": r["plex_guid"],
            "title": r["title"],
            "file_path": r["file_path"],
            "segments": [],
        }
        for r in job_rows
    }
    for row in seg_rows:
        vid = int(row["version_id"])
        if vid in job_map:
            job_map[vid]["segments"].append(dict(row))

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



# ── Hayaa sync (#222, #233) ────────────────────────────────────────────────────

async def get_hayaa_sync_titles() -> list[dict]:
    """Return every single-file non-ignored media version Hayaa can identify.

    Each single-file version of a title is its own Hayaa title under its own content key.
    Multi-part versions are left out because their segment times span several files.
    """
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT v.id AS version_id, v.plex_guid, j.title, v.file_path, "
            "COALESCE(v.duration_ms, 0) AS duration_ms, v.status, "
            "COALESCE(v.scan_version, 0) AS scan_version, v.finished_at, "
            "COALESCE(v.content_key, '') AS content_key, COALESCE(v.content_size, 0) AS content_size, "
            "COALESCE(v.hayaa_sync_state, 'pending') AS hayaa_sync_state, "
            "COALESCE(v.resolution, '') AS resolution "
            "FROM title_versions v JOIN scan_jobs j ON j.plex_guid = v.plex_guid "
            "WHERE COALESCE(v.file_path, '') != '' AND COALESCE(j.ignored, 0) = 0 "
            "AND COALESCE(json_array_length(NULLIF(v.part_files, '')), 0) <= 1"
        )
    return [dict(r) for r in rows]


async def get_hayaa_removed_titles() -> list[dict]:
    """Return removed single-file versions whose content key was kept, for upload-only sync.

    A path a live version now uses is left out: its key may belong to the new file.
    """
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT version_id, plex_guid, title, file_path, COALESCE(duration_ms, 0) AS duration_ms, "
            "COALESCE(status, '') AS status, COALESCE(scan_version, 0) AS scan_version, finished_at, "
            "content_key, content_size AS size FROM removed_titles "
            "WHERE COALESCE(content_key, '') != '' AND COALESCE(file_path, '') != '' "
            "AND COALESCE(json_array_length(NULLIF(part_files, '')), 0) <= 1 "
            "AND NOT EXISTS (SELECT 1 FROM title_versions v WHERE v.file_path = removed_titles.file_path)"
        )
    return [dict(r) for r in rows]


async def get_removed_segments_for_hayaa(targets: list[int] | list[str]) -> list[dict]:
    """get_segments_for_hayaa for removed versions/titles: their segments live in removed_segments."""
    if not targets:
        return []
    col = "version_id" if isinstance(targets[0], int) else "plex_guid"
    out: list[dict] = []
    async with get_connection() as conn:
        for i in range(0, len(targets), 500):
            chunk = targets[i:i + 500]
            rows = await conn.execute_fetchall(
                f"""
                SELECT s.*, COALESCE(f.labels, s.labels) AS detected_labels,
                       COALESCE(f.corrected_labels, '') AS corrected_labels
                FROM removed_segments s
                LEFT JOIN segment_feedback f ON f.segment_id = s.id AND f.verdict = 'relabeled'
                WHERE s.{col} IN ({_in(chunk)})
                ORDER BY s.{col}, s.start_ms
                """,
                chunk,
            )
            out += [dict(r) for r in rows]
    return out


async def get_hayaa_titles() -> dict[str, dict]:
    """Return every known Hayaa title row, keyed by content key."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall("SELECT * FROM hayaa_titles")
    return {r["title_key"]: dict(r) for r in rows}


async def upsert_hayaa_titles(pairs: list[tuple]) -> None:
    """Record (content key, plex_guid[, version_id]) tuples; a key moving keeps its version."""
    if not pairs:
        return
    norm = [(p[0], p[1], p[2] if len(p) > 2 else 0) for p in pairs]
    async with get_connection() as conn:
        await conn.executemany(
            "INSERT INTO hayaa_titles(title_key, plex_guid, version_id) VALUES(?,?,?) "
            "ON CONFLICT(title_key) DO UPDATE SET plex_guid=excluded.plex_guid, "
            "version_id=CASE WHEN excluded.version_id > 0 THEN excluded.version_id ELSE hayaa_titles.version_id END",
            norm,
        )
        await conn.commit()


async def set_hayaa_title_versions(versions: dict[str, str]) -> None:
    """Store the consensus version pulled for each title key."""
    if not versions:
        return
    async with get_connection() as conn:
        await conn.executemany(
            "UPDATE hayaa_titles SET version=?, pulled_at=CURRENT_TIMESTAMP WHERE title_key=?",
            [(v, k) for k, v in versions.items()],
        )
        await conn.commit()


async def mark_hayaa_described(keys: list[str]) -> None:
    """Flag title keys whose media descriptor was sent."""
    async with get_connection() as conn:
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            await conn.execute(f"UPDATE hayaa_titles SET described=1 WHERE title_key IN ({_in(chunk)})", chunk)
        await conn.commit()


async def get_segments_for_hayaa(targets: list[int] | list[str]) -> list[dict]:
    """Return every segment of the given versions (or guids), false positives included, for reconciling."""
    if not targets:
        return []
    col = "version_id" if isinstance(targets[0], int) else "plex_guid"
    out: list[dict] = []
    async with get_connection() as conn:
        for i in range(0, len(targets), 500):
            chunk = targets[i:i + 500]
            rows = await conn.execute_fetchall(
                f"""
                SELECT s.*, COALESCE(f.labels, s.labels) AS detected_labels,
                       COALESCE(f.corrected_labels, '') AS corrected_labels
                FROM segments s
                LEFT JOIN segment_feedback f ON f.segment_id = s.id AND f.verdict = 'relabeled'
                WHERE s.{col} IN ({_in(chunk)})
                ORDER BY s.{col}, s.start_ms
                """,
                chunk,
            )
            out += [dict(r) for r in rows]
    return out


async def get_hayaa_sent(title_keys: list[str]) -> list[dict]:
    """Return the sent-ledger rows of the given title keys."""
    out: list[dict] = []
    async with get_connection() as conn:
        for i in range(0, len(title_keys), 500):
            chunk = title_keys[i:i + 500]
            rows = await conn.execute_fetchall(f"SELECT * FROM hayaa_sent WHERE title_key IN ({_in(chunk)})", chunk)
            out += [dict(r) for r in rows]
    return out


async def record_hayaa_sent(rows: list[tuple[str, str, str, str]], retracted: list[str]) -> None:
    """Record sent observations (obs_id, base_id, title_key, kind) and mark retracted ids not live."""
    async with get_connection() as conn:
        await conn.executemany(
            "INSERT OR IGNORE INTO hayaa_sent(obs_id, base_id, title_key, kind) VALUES(?,?,?,?)", rows
        )
        for i in range(0, len(retracted), 500):
            chunk = retracted[i:i + 500]
            await conn.execute(f"UPDATE hayaa_sent SET live=0 WHERE obs_id IN ({_in(chunk)})", chunk)
        await conn.commit()


async def replace_hayaa_claims(
    title_key: str, plex_guid: str, claims: list[dict], version_id: int = 0
) -> None:
    """Replace a version's staged claims with a pulled view.

    A claim that keeps its id and bounds keeps its verification result; one whose bounds moved
    is verified again. claims use ms and a JSON 'machine' string.
    """
    async with get_connection() as conn:
        vid = int(version_id or 0)
        if vid <= 0:
            vrow = await (await conn.execute(
                "SELECT id FROM title_versions WHERE content_key=? AND COALESCE(content_key, '') != '' LIMIT 1",
                (title_key,),
            )).fetchone()
            if not vrow:
                vrow = await (await conn.execute(
                    "SELECT version_id AS id FROM hayaa_titles WHERE title_key=? AND version_id > 0 LIMIT 1",
                    (title_key,),
                )).fetchone()
            if not vrow:
                vrows = await conn.execute_fetchall(
                    "SELECT id FROM title_versions WHERE plex_guid=? ORDER BY id LIMIT 2",
                    (plex_guid,),
                )
                if len(vrows) == 1:
                    vrow = vrows[0]
            vid = int(vrow["id"]) if vrow else 0
        old = {
            r["claim_id"]: dict(r) for r in await conn.execute_fetchall(
                "SELECT * FROM hayaa_claims WHERE title_key=?", (title_key,)
            )
        }
        await conn.execute("DELETE FROM hayaa_claims WHERE title_key=?", (title_key,))
        for c in claims:
            prev = old.get(c["claim_id"])
            keep = prev is not None and prev["start_ms"] == c["start_ms"] and prev["end_ms"] == c["end_ms"]
            await conn.execute(
                "INSERT INTO hayaa_claims(title_key, claim_id, plex_guid, version_id, start_ms, end_ms, status, basis, labels, "
                "category, severity, machine, supporters, rejecters, verify_state, verify_conf, verify_family, "
                "verify_fp_drop, verify_thumb, verify_labels, decision) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (title_key, c["claim_id"], plex_guid, vid, c["start_ms"], c["end_ms"], c["status"], c["basis"],
                 c["labels"], c["category"], c["severity"], c["machine"], c["supporters"], c["rejecters"],
                 prev["verify_state"] if keep else "", prev["verify_conf"] if keep else None,
                 prev["verify_family"] if keep else "", prev["verify_fp_drop"] if keep else 0,
                 prev["verify_thumb"] if keep else "", prev["verify_labels"] if keep else "",
                 prev["decision"] if prev else ""),
            )
        await conn.commit()


async def get_hayaa_claims(plex_guids: list[str] | None = None, version_ids: list[int] | None = None) -> list[dict]:
    """Return staged claims, for the given versions/titles or all of them, by title and start."""
    async with get_connection() as conn:
        if version_ids is not None:
            out: list[dict] = []
            for i in range(0, len(version_ids), 500):
                chunk = version_ids[i:i + 500]
                rows = await conn.execute_fetchall(
                    f"SELECT * FROM hayaa_claims WHERE version_id IN ({_in(chunk)}) ORDER BY version_id, start_ms",
                    chunk,
                )
                out += [dict(r) for r in rows]
            return out
        if plex_guids is None:
            rows = await conn.execute_fetchall("SELECT * FROM hayaa_claims ORDER BY plex_guid, start_ms")
            return [dict(r) for r in rows]
        out = []
        for i in range(0, len(plex_guids), 500):
            chunk = plex_guids[i:i + 500]
            rows = await conn.execute_fetchall(
                f"SELECT * FROM hayaa_claims WHERE plex_guid IN ({_in(chunk)}) ORDER BY plex_guid, start_ms", chunk
            )
            out += [dict(r) for r in rows]
        return out


async def get_hayaa_claims_to_verify(limit: int) -> list[dict]:
    """Return claims waiting for a local re-check (verify_state 'pending'), with the claim version's file."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT c.*, COALESCE(v.file_path, j.file_path) AS file_path, j.title, j.rating_key, "
            "CASE WHEN COALESCE(v.duration_ms, 0) > 0 THEN v.duration_ms ELSE COALESCE(j.duration_ms, 0) END AS duration_ms "
            "FROM hayaa_claims c "
            "LEFT JOIN title_versions v ON v.id = c.version_id "
            "LEFT JOIN scan_jobs j ON j.plex_guid = c.plex_guid "
            "WHERE c.verify_state = 'pending' AND COALESCE(v.file_path, j.file_path, '') != '' "
            "ORDER BY c.updated_at LIMIT ?",
            (limit,),
        )
    return [dict(r) for r in rows]


async def count_hayaa_claims(verify_state: str | None = None, decision: str | None = None) -> int:
    """Count staged claims, optionally by verify state and/or apply decision."""
    where, args = [], []
    if verify_state is not None:
        where.append("verify_state=?")
        args.append(verify_state)
    if decision is not None:
        where.append("decision=?")
        args.append(decision)
    sql = "SELECT COUNT(*) FROM hayaa_claims" + (f" WHERE {' AND '.join(where)}" if where else "")
    async with get_connection() as conn:
        return (await (await conn.execute(sql, args)).fetchone())[0]


async def update_hayaa_claim(title_key: str, claim_id: str, **fields) -> None:
    """Set verify_* or decision fields of one staged claim."""
    allowed = {"verify_state", "verify_conf", "verify_family", "verify_fp_drop", "verify_thumb", "verify_labels",
               "decision"}
    cols = [k for k in fields if k in allowed]
    if not cols:
        return
    async with get_connection() as conn:
        await conn.execute(
            f"UPDATE hayaa_claims SET {', '.join(f'{c}=?' for c in cols)}, updated_at=CURRENT_TIMESTAMP "
            "WHERE title_key=? AND claim_id=?",
            [fields[c] for c in cols] + [title_key, claim_id],
        )
        await conn.commit()


async def get_hayaa_overrides(title_keys: list[str] | None = None) -> dict[tuple[str, str], str]:
    """Return the user's verdicts {(title_key, claim_id): verdict}."""
    async with get_connection() as conn:
        if title_keys is None:
            rows = await conn.execute_fetchall("SELECT * FROM hayaa_overrides")
        else:
            rows = []
            for i in range(0, len(title_keys), 500):
                chunk = title_keys[i:i + 500]
                rows += await conn.execute_fetchall(
                    f"SELECT * FROM hayaa_overrides WHERE title_key IN ({_in(chunk)})", chunk
                )
    return {(r["title_key"], r["claim_id"]): r["verdict"] for r in rows}


async def set_hayaa_override(title_key: str, claim_id: str, verdict: str | None) -> None:
    """Record the user's verdict on a claim ('keep', 'delete', 'restore'); None clears it."""
    async with get_connection() as conn:
        if verdict is None:
            await conn.execute("DELETE FROM hayaa_overrides WHERE title_key=? AND claim_id=?", (title_key, claim_id))
        else:
            await conn.execute(
                "INSERT OR REPLACE INTO hayaa_overrides(title_key, claim_id, verdict) VALUES(?,?,?)",
                (title_key, claim_id, verdict),
            )
        await conn.commit()


async def get_hayaa_title_key(plex_guid: str, version_id: int | None = None) -> str | None:
    """Return the content key Hayaa knows a version (or title) by, or None."""
    async with get_connection() as conn:
        if version_id is not None and version_id > 0:
            row = await (await conn.execute(
                "SELECT title_key FROM hayaa_titles WHERE version_id=? LIMIT 1", (version_id,)
            )).fetchone()
            if row:
                return row[0]
            vrow = await (await conn.execute(
                "SELECT content_key FROM title_versions WHERE id=? AND COALESCE(content_key, '') != ''",
                (version_id,),
            )).fetchone()
            if vrow:
                return vrow[0]
        row = await (await conn.execute(
            "SELECT title_key FROM hayaa_titles WHERE plex_guid=? LIMIT 1", (plex_guid,)
        )).fetchone()
    return row[0] if row else None


async def fill_hayaa_segment_labels(segment_id: int, labels: str) -> bool:
    """Give a pulled (source='hayaa') segment without labels the labels its local re-check found.

    Never overwrites labels already set (a user relabel or an earlier fill). Returns True if updated.
    """
    if not labels:
        return False
    async with get_connection() as conn:
        cur = await conn.execute(
            "UPDATE segments SET labels=? WHERE id=? AND source='hayaa' AND COALESCE(labels, '')=''",
            (labels, segment_id),
        )
        await conn.commit()
        return cur.rowcount > 0


async def set_segment_hayaa_claims(pairs: list[tuple[str, int]]) -> None:
    """Link segments to Hayaa claims: (claim_id or '', segment_id) pairs."""
    if not pairs:
        return
    async with get_connection() as conn:
        await conn.executemany("UPDATE segments SET hayaa_claim_id=? WHERE id=?", pairs)
        await conn.commit()


async def set_segments_false_positive(segment_ids: list[int], value: int) -> None:
    """Set false_positive on segments (Hayaa: 3 hides a community-rejected one, 0 restores it)."""
    async with get_connection() as conn:
        for i in range(0, len(segment_ids), 500):
            chunk = segment_ids[i:i + 500]
            await conn.execute(f"UPDATE segments SET false_positive=? WHERE id IN ({_in(chunk)})", [value, *chunk])
        await conn.commit()


async def get_hayaa_review(limit: int = 200) -> list[dict]:
    """Return claims held for review (decision 'hold' or 'verify') without a user verdict, with the title."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT c.title_key, c.claim_id, c.plex_guid, c.start_ms, c.end_ms, c.status, c.basis, c.labels, "
            "c.category, c.severity, c.supporters, c.rejecters, c.verify_state, c.verify_thumb, c.decision, "
            "COALESCE(j.title, '') AS title FROM hayaa_claims c "
            "LEFT JOIN scan_jobs j ON j.plex_guid = c.plex_guid "
            "LEFT JOIN hayaa_overrides o ON o.title_key = c.title_key AND o.claim_id = c.claim_id "
            "WHERE c.decision IN ('hold', 'verify') AND o.verdict IS NULL "
            "ORDER BY j.title, c.start_ms LIMIT ?",
            (limit,),
        )
    return [dict(r) for r in rows]


async def get_community_false_positives(limit: int = 200) -> list[dict]:
    """Return segments hidden because Hayaa's community rejected their claim (false_positive = 3)."""
    async with get_connection() as conn:
        rows = await conn.execute_fetchall(
            "SELECT id, plex_guid, title, start_ms, end_ms, labels, severity, source, thumbnail_path "
            "FROM segments WHERE false_positive = 3 ORDER BY title, start_ms LIMIT ?",
            (limit,),
        )
    return [dict(r) for r in rows]
