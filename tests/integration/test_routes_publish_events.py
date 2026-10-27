"""Mutating routes announce what changed (issue #89)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from cleanplex import database as db
from cleanplex import events

pytestmark = pytest.mark.usefixtures("setup_db")


def _drain(q):
    return [q.get_nowait() for _ in range(q.qsize())]


async def test_deleting_a_segment_announces_its_title(http_client):
    seg_id = await db.insert_segment("guid-ev", "Movie", start_ms=0, end_ms=1000)
    with events.subscribe() as q:
        await http_client.delete(f"/api/segments/{seg_id}")

    assert ("segments_changed", {"guid": "guid-ev"}) in _drain(q)


async def test_saving_settings_announces_it(http_client):
    with events.subscribe() as q:
        await http_client.put("/api/settings", json={"log_level": "INFO"})

    assert ("settings_changed", {}) in _drain(q)


async def test_changing_user_categories_announces_the_user(http_client):
    with events.subscribe() as q:
        await http_client.put("/api/users/alice/categories/nudity", json={"level": 2})

    assert ("users_changed", {"username": "alice"}) in _drain(q)


async def test_queueing_a_rescan_announces_a_scanner_change(http_client):
    await db.upsert_scan_job(plex_guid="g", title="T", file_path="/x", rating_key="1",
                             library_id="lib", library_title="Lib")
    with events.subscribe() as q, \
         patch("cleanplex.web.routes.scanner_routes.scan_mod.enqueue", new=AsyncMock()):
        await http_client.post("/api/scan/titles", json={"plex_guids": ["g"]})

    assert ("scanner_changed", {}) in _drain(q)


async def test_ignoring_a_title_announces_its_library(http_client):
    await db.upsert_scan_job(plex_guid="g", title="T", file_path="/x", rating_key="1",
                             library_id="lib9", library_title="Lib")
    with events.subscribe() as q:
        await http_client.post("/api/scan/title/g/ignore", json={"ignored": True})

    assert ("titles_changed", {"library_id": "lib9"}) in _drain(q)
