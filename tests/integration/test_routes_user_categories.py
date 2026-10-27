"""Integration tests for per-user category preference routes (issue #74)."""

from __future__ import annotations

import pytest

from cleanplex import database as db

pytestmark = pytest.mark.usefixtures("setup_db")


async def test_new_user_renders_at_the_inherited_defaults(http_client):
    """No stored prefs means 'filter everything', so controls show full strictness."""
    resp = await http_client.get("/api/users/alice/categories")
    body = resp.json()

    assert resp.status_code == 200
    assert body["uses_defaults"] is True
    assert all(c["level"] == 3 for c in body["categories"])


async def test_stored_prefs_are_returned(http_client):
    await db.upsert_user_category_pref("alice", "nudity", 3)

    body = (await http_client.get("/api/users/alice/categories")).json()
    levels = {c["category"]: c["level"] for c in body["categories"]}

    assert body["uses_defaults"] is False
    assert levels["nudity"] == 3
    # Unset categories stay at Everything once another one is saved (#77).
    assert levels["violence"] == 3


async def test_unset_categories_report_everything_after_a_change(http_client):
    await db.upsert_user_category_pref("alice", "language", 1)

    body = (await http_client.get("/api/users/alice/categories")).json()
    levels = {c["category"]: c["level"] for c in body["categories"]}

    assert levels["language"] == 1
    assert all(level == 3 for cat, level in levels.items() if cat != "language")


async def test_categories_report_whether_segments_exist(http_client):
    await db.insert_segment("guid-1", "Movie", 0, 1000, category="nudity")

    body = (await http_client.get("/api/users/alice/categories")).json()
    has = {c["category"]: c["has_segments"] for c in body["categories"]}

    assert has["nudity"] is True
    assert has["violence"] is False


async def test_updating_a_level_persists(http_client):
    resp = await http_client.put("/api/users/alice/categories/nudity", json={"level": 2})

    assert resp.status_code == 200
    assert (await db.get_user_category_prefs("alice"))["nudity"]["level"] == 2


async def test_action_override_persists(http_client):
    await http_client.put(
        "/api/users/alice/categories/language", json={"level": 3, "action": "mute"}
    )

    assert (await db.get_user_category_prefs("alice"))["language"]["action"] == "mute"


async def test_unknown_category_is_rejected(http_client):
    resp = await http_client.put("/api/users/alice/categories/banana", json={"level": 1})

    assert resp.status_code == 422


async def test_out_of_range_level_is_rejected(http_client):
    resp = await http_client.put("/api/users/alice/categories/nudity", json={"level": 9})

    assert resp.status_code == 422


async def test_unsupported_action_is_rejected(http_client):
    resp = await http_client.put(
        "/api/users/alice/categories/nudity", json={"level": 1, "action": "blur"}
    )

    assert resp.status_code == 422


async def test_reset_clears_stored_prefs(http_client):
    await db.upsert_user_category_pref("alice", "nudity", 3)

    resp = await http_client.delete("/api/users/alice/categories")

    assert resp.json()["removed"] == 1
    assert (await http_client.get("/api/users/alice/categories")).json()["uses_defaults"] is True
