"""Tests for syncing Plex cast and serving it for the Library actor filter (#180)."""

from types import SimpleNamespace

from cleanplex import cast_sync
from cleanplex import database as db
from cleanplex.plex_client import PlexClient


async def _job(guid, rating_key, library="1", media_type="movie", show_rating_key=""):
    await db.upsert_scan_job(guid, guid, f"/m/{guid}.mkv", rating_key, library, "Lib",
                             media_type=media_type, show_rating_key=show_rating_key)


async def test_keys_to_sync_use_the_show_for_episodes_and_skip_synced_ones(setup_db):
    await _job("m1", "10")
    await _job("e1", "21", media_type="episode", show_rating_key="20")
    await _job("e2", "22", media_type="episode", show_rating_key="20")

    assert sorted(await db.get_cast_keys_to_sync(50)) == ["10", "20"]  # one key for the whole show

    await db.replace_cast({"10": [], "20": ["A"]})  # an empty cast still counts as synced

    assert await db.get_cast_keys_to_sync(50) == []


async def test_keys_to_sync_returns_nothing_for_an_empty_database(setup_db):
    assert await db.get_cast_keys_to_sync(50) == []


async def test_cast_for_library_maps_episodes_to_their_shows_cast(setup_db):
    await _job("m1", "10")
    await _job("e1", "21", media_type="episode", show_rating_key="20")
    await _job("other", "30", library="2")
    await db.replace_cast({"10": ["Jane Roe", "John Doe"], "20": ["Ann Lee"], "30": ["Zed"]})

    cast = await db.get_cast_for_library("1")

    assert cast == {"m1": ["Jane Roe", "John Doe"], "e1": ["Ann Lee"]}


async def test_replace_cast_replaces_the_previous_cast(setup_db):
    await _job("m1", "10")
    await db.replace_cast({"10": ["Old"]})
    await db.replace_cast({"10": ["New"]})

    assert (await db.get_cast_for_library("1"))["m1"] == ["New"]


async def test_get_cast_reads_roles_in_chunks():
    calls = []

    def fetch_items(path):
        calls.append(path)
        return [SimpleNamespace(ratingKey=int(k), roles=[SimpleNamespace(tag=f"Actor{k}"), SimpleNamespace(tag="")])
                for k in path.rsplit("/", 1)[1].split(",")]

    client = PlexClient("http://x", "t")
    client._server = SimpleNamespace(fetchItems=fetch_items)

    out = await client.get_cast([str(i) for i in range(1, 46)])

    assert len(calls) == 2  # 45 keys, 40 per request
    assert out["1"] == ["Actor1"] and out["45"] == ["Actor45"]


async def test_sync_cast_once_stores_what_plex_returns(setup_db):
    await _job("m1", "10")

    class FakeClient:
        async def get_cast(self, keys):
            return {k: ["Jane Roe"] for k in keys}

    assert await cast_sync.sync_cast_once(lambda: FakeClient()) == 1
    assert await cast_sync.sync_cast_once(lambda: FakeClient()) == 0  # nothing left
    assert (await db.get_cast_for_library("1"))["m1"] == ["Jane Roe"]


async def test_get_cast_survives_a_rating_key_plex_no_longer_has():
    """One missing key 404s its whole batch; it must not block the others (#180)."""
    from plexapi.exceptions import NotFound

    def fetch_items(path):
        keys = path.rsplit("/", 1)[1].split(",")
        if "2" in keys:
            if len(keys) > 1:
                raise NotFound("404")
            raise NotFound("404")
        return [SimpleNamespace(ratingKey=int(k), roles=[SimpleNamespace(tag=f"A{k}")]) for k in keys]

    client = PlexClient("http://x", "t")
    client._server = SimpleNamespace(fetchItems=fetch_items)

    out = await client.get_cast(["1", "2", "3"])

    assert out == {"1": ["A1"], "2": [], "3": ["A3"]}  # the missing key is recorded as having no cast
