"""Daily online backup of the SQLite database, with retention for backups and old deploy-time copies."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from pathlib import Path

from . import database as db
from .logger import get_logger

logger = get_logger(__name__)

BACKUP_INTERVAL_S = 24 * 3600
STARTUP_DELAY_S = 300
KEEP_BACKUPS = 7
# Manual copies made before a deploy (cleanplex.db.bak-<stamp>) are kept this many, newest first.
KEEP_DEPLOY_COPIES = 3


def backup_dir() -> Path:
    return db.get_db_path().parent / "backups"


def _prune(directory: Path, pattern: str, keep: int) -> int:
    """Delete all but the newest *keep* files matching *pattern* (and their -shm/-wal siblings)."""
    files = sorted(
        (p for p in directory.glob(pattern) if not p.name.endswith(("-shm", "-wal"))),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for old in files[keep:]:
        for victim in (old, old.with_name(old.name + "-shm"), old.with_name(old.name + "-wal")):
            try:
                victim.unlink()
            except FileNotFoundError:
                continue
        removed += 1
    return removed


async def backup_once() -> Path:
    """Write a consistent copy with VACUUM INTO, then prune old backups; return the new file."""
    target_dir = backup_dir()
    await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)
    target = target_dir / f"cleanplex-{datetime.now():%Y%m%d-%H%M%S}.db"
    async with db.get_connection() as conn:
        # VACUUM INTO reads a snapshot, so scans and UI writes can continue meanwhile.
        await conn.execute("VACUUM INTO ?", (str(target),))
    await asyncio.to_thread(_prune, target_dir, "cleanplex-*.db", KEEP_BACKUPS)
    await asyncio.to_thread(_prune, db.get_db_path().parent, "cleanplex.db.bak-*", KEEP_DEPLOY_COPIES)
    return target


def _latest_age_s(directory: Path) -> float | None:
    files = list(directory.glob("cleanplex-*.db")) if directory.is_dir() else []
    if not files:
        return None
    return time.time() - max(os.stat(p).st_mtime for p in files)


async def db_backup_loop() -> None:
    """Back up once a day; skips startup if a backup under a day old exists, so restarts do not pile up copies."""
    await asyncio.sleep(STARTUP_DELAY_S)
    while True:
        try:
            age = await asyncio.to_thread(_latest_age_s, backup_dir())
            if age is None or age >= BACKUP_INTERVAL_S:
                path = await backup_once()
                logger.info("Database backup written to %s", path)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Database backup failed")
        await asyncio.sleep(BACKUP_INTERVAL_S)
