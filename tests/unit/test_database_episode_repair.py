"""Episodes stored as movies are repaired (#246)."""

from __future__ import annotations

import pytest

from cleanplex import database as db

pytestmark = pytest.mark.usefixtures("setup_db")


async def _row(guid: str) -> dict:
    async with db.get_connection() as conn:
        r = await (await conn.execute(
            "SELECT media_type, show_guid, show_rating_key FROM scan_jobs WHERE plex_guid=?", (guid,)
        )).fetchone()
    return dict(r)


def _job(guid: str, **kw) -> dict:
    base = {"plex_guid": guid, "title": guid, "file_path": f"/m/{guid}.mkv", "rating_key": "1",
            "library_id": "2", "library_title": "TV Shows"}
    return {**base, **kw}


async def test_startup_repairs_episodes_stored_as_movies():
    await db.upsert_scan_jobs_batch([_job("ep", media_type="movie", show_guid="plex://show/x",
                                          show_rating_key="9")])
    await db.upsert_scan_jobs_batch([_job("film", media_type="movie")])
    await db.init_db()
    assert (await _row("ep"))["media_type"] == "episode"
    assert (await _row("film"))["media_type"] == "movie"


async def test_sync_heals_an_existing_row_that_lost_its_show():
    await db.upsert_scan_jobs_batch([_job("ep", media_type="movie")])
    await db.upsert_scan_jobs_batch([_job("ep", media_type="episode", show_guid="plex://show/x",
                                          show_rating_key="9")])
    assert await _row("ep") == {"media_type": "episode", "show_guid": "plex://show/x",
                                "show_rating_key": "9"}


async def test_sync_leaves_movies_alone():
    await db.upsert_scan_jobs_batch([_job("film", media_type="movie", title="Old title")])
    await db.upsert_scan_jobs_batch([_job("film", media_type="movie", title="New title")])
    assert (await _row("film"))["media_type"] == "movie"
