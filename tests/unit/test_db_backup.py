"""Tests for the daily database backup and its retention."""

import os
import sqlite3
import time

import pytest

from cleanplex import database as db
from cleanplex import db_backup


@pytest.fixture
def db_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "cleanplex.db")
    return tmp_path


async def test_backup_once_writes_a_readable_copy(db_dir):
    await db.init_db()
    await db.set_setting("plex_url", "http://x")
    path = await db_backup.backup_once()
    row = sqlite3.connect(path).execute("select value from settings where key='plex_url'").fetchone()
    assert row == ("http://x",)


async def test_backup_once_keeps_only_the_newest_backups(db_dir, monkeypatch):
    await db.init_db()
    monkeypatch.setattr(db_backup, "KEEP_BACKUPS", 2)
    folder = db_dir / "backups"
    folder.mkdir()
    for i in range(4):
        f = folder / f"cleanplex-2020010{i}-000000.db"
        f.write_bytes(b"x")
        os.utime(f, (time.time() - 1000 + i, time.time() - 1000 + i))
    await db_backup.backup_once()
    names = sorted(p.name for p in folder.glob("cleanplex-*.db"))
    assert len(names) == 2
    assert "cleanplex-20200103-000000.db" in names


async def test_backup_once_prunes_old_deploy_copies_with_siblings(db_dir, monkeypatch):
    await db.init_db()
    monkeypatch.setattr(db_backup, "KEEP_DEPLOY_COPIES", 1)
    for i in range(3):
        for suffix in ("", "-shm", "-wal"):
            f = db_dir / f"cleanplex.db.bak-{i}{suffix}"
            f.write_bytes(b"x")
            os.utime(f, (time.time() - 100 + i, time.time() - 100 + i))
    await db_backup.backup_once()
    assert sorted(p.name for p in db_dir.glob("cleanplex.db.bak-*")) == [
        "cleanplex.db.bak-2", "cleanplex.db.bak-2-shm", "cleanplex.db.bak-2-wal",
    ]
