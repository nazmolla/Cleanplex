"""Unit tests for the per-connection pragmas set by database.get_connection()."""

from __future__ import annotations

import asyncio

import pytest

from cleanplex import database as db


pytestmark = pytest.mark.usefixtures("setup_db")


async def test_connection_sets_busy_timeout_and_synchronous_normal():
    async with db.get_connection() as conn:
        timeout = (await (await conn.execute("PRAGMA busy_timeout")).fetchone())[0]
        sync = (await (await conn.execute("PRAGMA synchronous")).fetchone())[0]
    assert timeout == db.BUSY_TIMEOUT_MS
    assert sync == 1  # NORMAL


async def test_writer_waits_for_held_write_lock_instead_of_failing():
    async with db.get_connection() as holder:
        await holder.execute("BEGIN IMMEDIATE")

        async def write_second():
            await db.set_setting("plex_url", "http://waited")

        task = asyncio.create_task(write_second())
        await asyncio.sleep(0.3)
        assert not task.done()  # blocked on the lock, not raised
        await holder.rollback()
    await asyncio.wait_for(task, timeout=5)
    assert await db.get_setting("plex_url") == "http://waited"
