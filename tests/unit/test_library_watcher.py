"""Tests for the library watcher's per-pass database access."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cleanplex import database as db
from cleanplex import watcher


def _item(guid):
    return SimpleNamespace(
        plex_guid=guid, title=guid, file_path=f"/m/{guid}.mkv", rating_key="1", library_id="l",
        library_title="L", content_rating="", media_type="movie", year=2020, show_guid="",
        part_files=[f"/m/{guid}.mkv"], duration_ms=1000,
    )


async def test_library_watcher_pass_queues_new_items_with_one_lookup_per_section(setup_db):
    await db.upsert_scan_job("known", "known", "/m/known.mkv", "1", "l", "L")
    client = MagicMock()
    client.get_library_sections = AsyncMock(return_value=[SimpleNamespace(section_id="l")])
    client.get_library_items = AsyncMock(return_value=[_item("known"), _item("new1"), _item("new2")])
    config = SimpleNamespace(is_configured=lambda: True)
    enqueue = AsyncMock()

    async def stop(_):
        raise asyncio.CancelledError

    with patch.object(watcher, "enqueue", enqueue), \
         patch.object(watcher, "_import_sidecar", new=AsyncMock(return_value=False)), \
         patch.object(db, "get_scan_job_by_guid", new=AsyncMock()) as per_item, \
         patch.object(watcher.asyncio, "sleep", stop), \
         pytest.raises(asyncio.CancelledError):
        await watcher.library_watcher_loop(AsyncMock(return_value=config), lambda: client)

    per_item.assert_not_called()
    assert sorted(c.args[0] for c in enqueue.call_args_list) == ["new1", "new2"]
    assert set(await db.get_scan_jobs_by_guids(["known", "new1", "new2"])) == {"known", "new1", "new2"}
