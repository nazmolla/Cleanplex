"""Tests for multiple Plex media versions per title (#233)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import cleanplex.hayaa as hayaa
import cleanplex.hayaa_sync as hs
import cleanplex.scanner as scanner
from cleanplex import database as db
from cleanplex import filter_engine, stream_proxy, watcher
from cleanplex.config import Config
from cleanplex.plex_client import ActiveSession, PlexClient


def _blake3_hex(data: bytes) -> str:
    return hayaa.content_key_for_bytes(data) if hasattr(hayaa, "content_key_for_bytes") else hashlib.blake2b(data, digest_size=32).hexdigest()


# ── Migration ─────────────────────────────────────────────────────────────────

async def test_migration_populates_title_versions_and_links_existing_data(setup_db, tmp_path):
    """Pre-#233 single-row scan_jobs/removed_titles/segments migrate in place and idempotently."""
    db_file = tmp_path / "pre233.db"
    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript("""
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE scan_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, plex_guid TEXT UNIQUE NOT NULL,
                title TEXT, file_path TEXT, part_files TEXT DEFAULT '', rating_key TEXT,
                library_id TEXT, library_title TEXT, media_type TEXT DEFAULT 'movie',
                content_rating TEXT DEFAULT '', year INTEGER, status TEXT DEFAULT 'done',
                progress REAL DEFAULT 1.0, force_scan INTEGER DEFAULT 0, ignored INTEGER DEFAULT 0,
                started_at TIMESTAMP, finished_at TIMESTAMP DEFAULT '2026-10-01 00:00:00',
                error_msg TEXT, duration_ms INTEGER DEFAULT 7200000, queue_pos INTEGER,
                scan_version INTEGER DEFAULT 4, content_key TEXT DEFAULT 'key_live_1',
                content_size INTEGER DEFAULT 1234, content_mtime INTEGER DEFAULT 5678,
                hayaa_sync_state TEXT DEFAULT 'synced', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE removed_titles (
                plex_guid TEXT PRIMARY KEY, title TEXT, rating_key TEXT, file_path TEXT,
                part_files TEXT DEFAULT '', duration_ms INTEGER DEFAULT 0, status TEXT DEFAULT '',
                scan_version INTEGER DEFAULT 0, finished_at TIMESTAMP, content_key TEXT DEFAULT '',
                content_size INTEGER DEFAULT 0, content_mtime INTEGER DEFAULT 0,
                removed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, purged_at TIMESTAMP
            );
            CREATE TABLE segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT, plex_guid TEXT NOT NULL, title TEXT,
                start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, confidence REAL DEFAULT 0,
                thumbnail_path TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                labels TEXT DEFAULT '', category TEXT DEFAULT 'nudity', severity TEXT DEFAULT 'high',
                action TEXT DEFAULT 'skip', channel TEXT DEFAULT 'both', source TEXT DEFAULT 'scanner',
                language TEXT DEFAULT '', detector TEXT DEFAULT '', false_positive INTEGER DEFAULT 0,
                hover_status TEXT DEFAULT '', thumbnail_ms INTEGER, refined_at TIMESTAMP,
                hayaa_claim_id TEXT DEFAULT ''
            );
            CREATE TABLE removed_segments (
                id INTEGER PRIMARY KEY, plex_guid TEXT NOT NULL, title TEXT,
                start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, confidence REAL DEFAULT 0,
                thumbnail_path TEXT, created_at TIMESTAMP, labels TEXT DEFAULT '',
                category TEXT DEFAULT 'nudity', severity TEXT DEFAULT 'high',
                action TEXT DEFAULT 'skip', channel TEXT DEFAULT 'both', source TEXT DEFAULT 'scanner',
                language TEXT DEFAULT '', detector TEXT DEFAULT '', false_positive INTEGER DEFAULT 0,
                hover_status TEXT DEFAULT '', thumbnail_ms INTEGER, refined_at TIMESTAMP
            );
            CREATE TABLE segment_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT, plex_guid TEXT NOT NULL, segment_id INTEGER,
                verdict TEXT NOT NULL DEFAULT 'false_positive', start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL, source TEXT DEFAULT '', detector TEXT DEFAULT '',
                labels TEXT DEFAULT '', corrected_labels TEXT DEFAULT '', confidence REAL DEFAULT 0,
                frame_path TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE keyframe_index (
                file_path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime INTEGER NOT NULL,
                keyframes TEXT NOT NULL, indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE hayaa_titles (
                title_key TEXT PRIMARY KEY, plex_guid TEXT NOT NULL, version TEXT DEFAULT '',
                described INTEGER DEFAULT 0, pulled_at TIMESTAMP
            );
            CREATE TABLE hayaa_claims (
                title_key TEXT NOT NULL, claim_id TEXT NOT NULL, plex_guid TEXT NOT NULL,
                start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, status TEXT NOT NULL,
                basis TEXT NOT NULL, labels TEXT DEFAULT '', category TEXT DEFAULT 'nudity',
                severity TEXT DEFAULT 'high', machine TEXT DEFAULT '{}', supporters INTEGER DEFAULT 0,
                rejecters INTEGER DEFAULT 0, verify_state TEXT DEFAULT '', verify_conf REAL,
                verify_family TEXT DEFAULT '', verify_fp_drop INTEGER DEFAULT 0, verify_thumb TEXT DEFAULT '',
                verify_labels TEXT DEFAULT '', decision TEXT DEFAULT '', updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (title_key, claim_id)
            );
            CREATE TABLE hayaa_sent (
                obs_id TEXT PRIMARY KEY, base_id TEXT NOT NULL, title_key TEXT NOT NULL,
                kind TEXT NOT NULL, live INTEGER DEFAULT 1, sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE title_parts (
                part_id INTEGER PRIMARY KEY, rating_key TEXT NOT NULL
            );
            CREATE TABLE proxy_sessions (
                session_id TEXT PRIMARY KEY, rating_key TEXT NOT NULL, username TEXT DEFAULT '',
                client_id TEXT DEFAULT '', client_title TEXT DEFAULT '', offset_ms INTEGER DEFAULT 0,
                ranges TEXT NOT NULL, spans TEXT NOT NULL, removed TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            INSERT INTO scan_jobs(plex_guid, title, file_path, rating_key, library_id, library_title)
                VALUES ('g-live', 'Live Movie', '/media/live_1080p.mkv', '100', '1', 'Movies');
            INSERT INTO segments(plex_guid, title, start_ms, end_ms) VALUES ('g-live', 'Live Movie', 10000, 15000);
            INSERT INTO segment_feedback(plex_guid, segment_id, start_ms, end_ms) VALUES ('g-live', 1, 10000, 15000);
            INSERT INTO keyframe_index(file_path, size, mtime, keyframes) VALUES ('/media/live_1080p.mkv', 1234, 5678, '[0, 5000]');
            INSERT INTO hayaa_titles(title_key, plex_guid) VALUES ('key_live_1', 'g-live');
            INSERT INTO hayaa_claims(title_key, claim_id, plex_guid, start_ms, end_ms, status, basis)
                VALUES ('key_live_1', 'c1', 'g-live', 10000, 15000, 'accepted', 'human');
            INSERT INTO hayaa_sent(obs_id, base_id, title_key, kind) VALUES ('o1', 'b1', 'key_live_1', 'detect');

            INSERT INTO removed_titles(plex_guid, title, file_path, content_key, content_size, content_mtime)
                VALUES ('g-rem', 'Old Movie', '/media/old.mkv', 'key_old_1', 999, 111);
            INSERT INTO removed_segments(id, plex_guid, title, start_ms, end_ms)
                VALUES (99, 'g-rem', 'Old Movie', 20000, 25000);
        """)
        await conn.commit()

    old_path = db.get_db_path()
    try:
        db.set_db_path(db_file)
        await db.init_db()
        await db.init_db()  # idempotent on second run

        versions = await db.get_title_versions("g-live")
        assert len(versions) == 1
        vid = versions[0]["id"]
        assert versions[0]["file_path"] == "/media/live_1080p.mkv"
        assert versions[0]["content_key"] == "key_live_1"
        assert versions[0]["hayaa_sync_state"] == "synced"
        assert versions[0]["scan_version"] == 4

        (seg,) = await db.get_segments_for_guid("g-live")
        assert seg["version_id"] == vid
        (claim,) = await db.get_hayaa_claims(["g-live"])
        assert claim["version_id"] == vid
        rem = await db.get_removed_title("g-rem")
        assert rem is not None and rem["version_id"] < 0
        rem_segs = await db.get_removed_segments("g-rem")
        assert rem_segs[0]["version_id"] == rem["version_id"]
    finally:
        db.set_db_path(old_path)


# ── PlexClient multi-version parsing ──────────────────────────────────────────

def test_plex_client_collects_multiple_versions_and_active_session_selection():
    part_1080 = SimpleNamespace(file="/media/Film.1080p.mkv", id=101, size=2_000_000)
    part_4k = SimpleNamespace(file="/media/Film.2160p.mkv", id=102, size=8_000_000)
    media_1080 = SimpleNamespace(id=501, videoResolution="1080", duration=6_000_000, parts=[part_1080])
    media_4k = SimpleNamespace(id=502, videoResolution="4k", duration=6_000_000, parts=[part_4k])
    plex_item = SimpleNamespace(
        guid="plex://movie/abc",
        guids=[],
        title="Film",
        thumb="",
        ratingKey=77,
        contentRating="R",
        type="movie",
        year=2025,
        duration=6_000_000,
        media=[media_1080, media_4k],
    )
    client = PlexClient("http://localhost:32400", "tok")
    item = client._media_item_from_plex(plex_item, "1", "Movies")
    assert item is not None
    assert len(item.versions) == 2
    assert item.versions[0].resolution == "1080"
    assert item.versions[0].plex_media_id == "501"
    assert item.versions[0].part_ids == [101]
    assert item.versions[1].resolution == "4k"
    assert item.versions[1].plex_media_id == "502"
    assert item.versions[1].part_ids == [102]
    assert item.part_ids == [101, 102]

    # Active session selects the 4K version specifically (selected=True)
    sess_item = SimpleNamespace(
        sessionKey="s1",
        session=[SimpleNamespace(id="ps1", key="/transcode/s1")],
        usernames=["alice"],
        ratingKey=77,
        guid="plex://movie/abc",
        guids=[],
        title="Film",
        type="movie",
        thumb="",
        viewOffset=30000,
        duration=6_000_000,
        players=[SimpleNamespace(state="playing", title="Apple TV", product="Plex for Apple TV", address="192.168.1.5", port=32500, machineIdentifier="cid1", local=True, remotePublicAddress="")],
        media=[
            SimpleNamespace(id=501, selected=False, parts=[SimpleNamespace(id=101, selected=False, file="/media/Film.1080p.mkv", streams=[], audioStreams=lambda: [])]),
            SimpleNamespace(id=502, selected=True, parts=[SimpleNamespace(id=102, selected=True, file="/media/Film.2160p.mkv", streams=[], audioStreams=lambda: [])]),
        ],
    )
    client._server = SimpleNamespace(sessions=lambda: [sess_item])
    import asyncio
    active = asyncio.run(client.get_active_sessions())
    assert len(active) == 1
    assert active[0].plex_media_id == "502"
    assert active[0].part_id == 102
    assert active[0].file_path == "/media/Film.2160p.mkv"


# ── Library sync: adding / removing individual versions ──────────────────────

async def test_library_watcher_adds_second_version_and_removes_one_without_losing_sibling(setup_db, tmp_path):
    f_1080 = tmp_path / "film_1080p.mkv"
    f_4k = tmp_path / "film_4k.mkv"
    f_1080.write_bytes(b"a" * 1000)
    f_4k.write_bytes(b"b" * 2000)

    def _make_item(versions_spec):
        return SimpleNamespace(
            plex_guid="g1", title="Film", file_path=versions_spec[0][1], rating_key="77",
            library_id="1", library_title="Movies", content_rating="R", media_type="movie",
            year=2025, show_guid="", part_files=[versions_spec[0][1]], duration_ms=60000,
            part_ids=[p for _, _, p in versions_spec],
            versions=[
                SimpleNamespace(
                    plex_media_id=mid, resolution=res, file_path=path,
                    part_files=[path], part_ids=[pid], file_size=1000, duration_ms=60000,
                )
                for mid, path, pid, res in [(m, p, pid, "1080" if "1080" in m else "4k") for m, p, pid in versions_spec]
            ],
        )

    # First pass: 1080p only
    item1 = _make_item([("m1080", str(f_1080), 101)])
    client = MagicMock()
    client.get_library_sections = AsyncMock(return_value=[SimpleNamespace(section_id="1")])
    client.get_library_items = AsyncMock(return_value=[item1])
    cfg = SimpleNamespace(is_configured=lambda: True)

    async def stop(_):
        raise asyncio.CancelledError

    import asyncio
    with patch.object(watcher, "enqueue_versions", new=AsyncMock()) as enq, \
         patch.object(watcher.asyncio, "sleep", stop), \
         pytest.raises(asyncio.CancelledError):
        await watcher.library_watcher_loop(AsyncMock(return_value=cfg), lambda: client)

    (v1080,) = await db.get_title_versions("g1")
    assert enq.await_args_list[0].args[0] == [v1080["id"]]
    # Finish scan and store segment & content key for 1080p
    await db.update_version_status(v1080["id"], "done", progress=1.0, scan_version=4)
    await db.set_title_content_key("g1", "ck_1080", 1000, 1, version_id=v1080["id"])
    seg_1080 = await db.insert_segment("g1", "Film", 5000, 9000, version_id=v1080["id"])

    # Second pass: user adds 4K version to Plex
    item2 = _make_item([("m1080", str(f_1080), 101), ("m4k", str(f_4k), 102)])
    client.get_library_items = AsyncMock(return_value=[item2])
    with patch.object(watcher, "enqueue_versions", new=AsyncMock()) as enq2, \
         patch.object(watcher.asyncio, "sleep", stop), \
         pytest.raises(asyncio.CancelledError):
        await watcher.library_watcher_loop(AsyncMock(return_value=cfg), lambda: client)

    versions = await db.get_title_versions("g1")
    assert len(versions) == 2
    v1080_after = next(v for v in versions if v["plex_media_id"] == "m1080")
    v4k = next(v for v in versions if v["plex_media_id"] == "m4k")
    # Existing 1080p scan state & key preserved, ONLY 4K queued!
    assert v1080_after["status"] == "done" and v1080_after["content_key"] == "ck_1080"
    assert v4k["status"] == "pending"
    assert enq2.await_args_list[0].args[0] == [v4k["id"]]
    assert await db.get_part_info(101) == ("77", v1080_after["id"])
    assert await db.get_part_info(102) == ("77", v4k["id"])

    # Scan 4K -> produces separate segment linked only to 4K
    seg_4k = await db.insert_segment("g1", "Film", 20000, 25000, version_id=v4k["id"])
    assert [s["id"] for s in await db.get_segments_for_version(v1080_after["id"])] == [seg_1080]
    assert [s["id"] for s in await db.get_segments_for_version(v4k["id"])] == [seg_4k]

    # Third pass: 1080p deleted from Plex, only 4K kept
    item3 = _make_item([("m4k", str(f_4k), 102)])
    client.get_library_items = AsyncMock(return_value=[item3])
    with patch.object(watcher, "enqueue_versions", new=AsyncMock()), \
         patch.object(watcher.asyncio, "sleep", stop), \
         pytest.raises(asyncio.CancelledError):
        await watcher.library_watcher_loop(AsyncMock(return_value=cfg), lambda: client)

    live_versions = await db.get_title_versions("g1")
    assert [v["id"] for v in live_versions] == [v4k["id"]]
    assert [s["id"] for s in await db.get_segments_for_guid("g1")] == [seg_4k]
    rem = await db.get_removed_title("g1")
    assert rem is not None and rem["version_id"] == v1080_after["id"] and rem["content_key"] == "ck_1080"
    assert [s["id"] for s in await db.get_removed_segments("g1")] == [seg_1080]


# ── Scanner isolation per version ─────────────────────────────────────────────

async def test_scanning_two_versions_creates_separate_segments_and_thumbnails(setup_db, tmp_path):
    f1 = tmp_path / "movie_1080.mkv"
    f2 = tmp_path / "movie_4k.mkv"
    f1.write_bytes(b"1" * 512)
    f2.write_bytes(b"2" * 512)
    await db.upsert_scan_jobs_batch([{
        "plex_guid": "g1", "title": "Dual", "file_path": str(f1), "rating_key": "9",
        "library_id": "1", "library_title": "Movies",
        "versions": [
            {"plex_media_id": "m1", "resolution": "1080", "file_path": str(f1), "duration_ms": 10000},
            {"plex_media_id": "m2", "resolution": "4k", "file_path": str(f2), "duration_ms": 10000},
        ],
    }])
    v1, v2 = await db.get_title_versions("g1")
    await db.set_force_scan("g1", True)

    async def batch_for(path, step_ms, duration_ms, size, hardware=False):
        if path == str(f1):
            yield 2000, b"hit-1080"
        else:
            yield 6000, b"hit-4k"

    def classify(jpeg, threshold, *a, **k):
        return True, 0.9, ["BUTTOCKS_EXPOSED"]

    thumbs = tmp_path / "thumbs"
    thumbs.mkdir()
    cfg = replace(Config(), scan_step_ms=2000, segment_gap_ms=1000, segment_min_hits=1, siglip_enabled=False, refine_step_ms=0)
    with patch.object(scanner, "extract_frames_batch", batch_for), \
         patch.object(scanner, "get_duration_ms", AsyncMock(return_value=10_000)), \
         patch.object(scanner, "_classify_frame", classify), \
         patch.object(scanner, "THUMBNAILS_DIR", thumbs):
        await scanner.scan_video("g1", cfg, version_id=v1["id"])
        await scanner.scan_video("g1", cfg, version_id=v2["id"])

    s1 = await db.get_segments_for_version(v1["id"])
    s2 = await db.get_segments_for_version(v2["id"])
    assert len(s1) == 1 and s1[0]["start_ms"] == 2000
    assert len(s2) == 1 and s2[0]["start_ms"] == 6000
    assert s1[0]["thumbnail_path"] != s2[0]["thumbnail_path"]
    assert f"_v{v1['id']}_" in s1[0]["thumbnail_path"]
    assert f"_v{v2['id']}_" in s2[0]["thumbnail_path"]


# ── Playback isolation: filter_engine & stream_proxy ──────────────────────────

async def test_filter_engine_never_mixes_segments_across_versions(setup_db):
    await db.upsert_scan_jobs_batch([{
        "plex_guid": "g1", "title": "Dual", "file_path": "/m/1080.mkv", "rating_key": "55",
        "library_id": "1", "library_title": "Movies",
        "versions": [
            {"plex_media_id": "m1080", "media_index": 0, "resolution": "1080", "file_path": "/m/1080.mkv", "part_ids": "[101]"},
            {"plex_media_id": "m4k", "media_index": 1, "resolution": "4k", "file_path": "/m/4k.mkv", "part_ids": "[102]"},
        ],
    }])
    v1080, v4k = await db.get_title_versions("g1")
    await db.replace_title_parts([(101, "55", v1080["id"]), (102, "55", v4k["id"])])
    # Only 1080p has a segment at 10s..20s; 4K is clean
    await db.insert_segment("g1", "Dual", 10_000, 20_000, version_id=v1080["id"])

    client = MagicMock()
    client.seek = AsyncMock(return_value=True)

    # Playing 4K -> must NOT seek!
    sess_4k = ActiveSession(
        session_key="k4", playback_session_id="p4", user="alice", title="Dual",
        full_title="Dual", media_type="movie", plex_guid="g1", rating_key="55",
        position_ms=12_000, duration_ms=100_000, client_identifier="c1", client_title="ATV",
        client_address="1.1.1.2", client_port=32500, is_controllable=True,
        plex_media_id="m4k", part_id=102, file_path="/m/4k.mkv",
    )
    await filter_engine.process(sess_4k, client)
    client.seek.assert_not_awaited()

    # Playing 1080p -> MUST seek past 20_000 + post_buffer!
    sess_1080 = replace(sess_4k, session_key="k1080", plex_media_id="m1080", part_id=101, file_path="/m/1080.mkv")
    await filter_engine.process(sess_1080, client, pre_buffer_ms=1000, post_buffer_ms=1000)
    client.seek.assert_awaited_once()
    assert client.seek.await_args.args[1] == 21_000


async def test_stream_proxy_decides_copies_and_refuses_parts_per_version(setup_db, tmp_path):
    f1 = tmp_path / "1080.mkv"
    f2 = tmp_path / "4k.mkv"
    f1.write_bytes(b"1" * 100)
    f2.write_bytes(b"2" * 100)
    await db.upsert_scan_jobs_batch([{
        "plex_guid": "g1", "title": "Dual", "file_path": str(f1), "rating_key": "88",
        "library_id": "1", "library_title": "Movies",
        "versions": [
            {"plex_media_id": "m1080", "media_index": 0, "resolution": "1080", "file_path": str(f1), "part_files": json.dumps([str(f1)]), "part_ids": "[801]"},
            {"plex_media_id": "m4k", "media_index": 1, "resolution": "4k", "file_path": str(f2), "part_files": json.dumps([str(f2)]), "part_ids": "[802]"},
        ],
    }])
    v1080, v4k = await db.get_title_versions("g1")
    await db.replace_title_parts([(801, "88", v1080["id"]), (802, "88", v4k["id"])])
    await db.insert_segment("g1", "Dual", 10_000, 15_000, version_id=v1080["id"])

    st1 = f1.stat()
    await db.put_keyframes(str(f1), st1.st_size, int(st1.st_mtime), [0, 5000, 10000, 20000])

    # Policy isolates decisions by version
    pol = stream_proxy.db_policy(ignore_client=True)
    d_1080 = await stream_proxy.decide(pol, "88", "cli", "10.0.0.2", "", version_id=v1080["id"])
    d_4k = await stream_proxy.decide(pol, "88", "cli", "10.0.0.2", "", version_id=v4k["id"])
    assert d_1080.inject is True and d_1080.ranges == [(7000, 18000)]
    assert d_4k.inject is False

    # Copy-mode keyframes are version-scoped (1080p indexed -> keyframes; 4K unindexed -> None)
    assert await stream_proxy.db_keyframes("88", version_id=v1080["id"]) == [0, 5000, 10000, 20000]
    assert await stream_proxy.db_keyframes("88", version_id=v4k["id"]) is None

    # /_proxy/authorize blocks 1080p direct download (403) and allows clean 4K direct download (204)
    seen_upstream = []

    def upstream(req: httpx.Request) -> httpx.Response:
        seen_upstream.append(dict(req.url.params))
        return httpx.Response(200, text="ok")

    app = stream_proxy.create_proxy_app(
        "http://plex.test", pol,
        transport=httpx.MockTransport(upstream),
        keyframes_provider=stream_proxy.db_keyframes,
        part_version_lookup=stream_proxy.db_part_version_lookup,
        media_version_lookup=stream_proxy.db_media_version_lookup,
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://proxy.test") as client:
            r1 = await client.get("/_proxy/authorize", headers={"x-original-uri": "/library/parts/801/1/file.mkv"})
            r2 = await client.get("/_proxy/authorize", headers={"x-original-uri": "/library/parts/802/1/file.mkv"})
            assert r1.status_code == 403
            assert r2.status_code == 204

            # /start.m3u8 with mediaIndex=0 -> edited & copy mode (directStream=1); mediaIndex=1 -> untouched
            await client.get("/video/:/transcode/universal/start.m3u8", params={
                "path": "/library/metadata/88", "session": "s-1080", "mediaIndex": "0", "protocol": "hls",
            })
            await client.get("/video/:/transcode/universal/start.m3u8", params={
                "path": "/library/metadata/88", "session": "s-4k", "mediaIndex": "1", "protocol": "hls",
            })
    assert seen_upstream[0]["directStream"] == "1"
    assert "directPlay" not in seen_upstream[1]


# ── Hayaa sync per single-file version ────────────────────────────────────────

async def test_hayaa_treats_each_single_file_version_independently_and_skips_multipart(setup_db, tmp_path):
    f1 = tmp_path / "1080.mkv"
    f2 = tmp_path / "4k.mkv"
    f1.write_bytes(b"a" * 5000)
    f2.write_bytes(b"b" * 6000)
    await db.upsert_scan_jobs_batch([{
        "plex_guid": "g-dual", "title": "Dual Film", "file_path": str(f1), "rating_key": "1",
        "library_id": "1", "library_title": "Movies",
        "versions": [
            {"plex_media_id": "m1", "resolution": "1080", "file_path": str(f1), "part_files": json.dumps([str(f1)]), "duration_ms": 100000},
            {"plex_media_id": "m2", "resolution": "4k", "file_path": str(f2), "part_files": json.dumps([str(f2)]), "duration_ms": 100000},
            {"plex_media_id": "m3", "resolution": "720", "file_path": str(f1), "part_files": json.dumps(["/p1.mkv", "/p2.mkv"]), "duration_ms": 100000},
        ],
    }])
    v1, v2, v_mp = await db.get_title_versions("g-dual")
    await db.update_version_status(v1["id"], "done", progress=1.0, scan_version=scanner.SCAN_VERSION)
    await db.update_version_status(v2["id"], "done", progress=1.0, scan_version=scanner.SCAN_VERSION)
    sync_titles = await db.get_hayaa_sync_titles()
    # Multi-part version excluded; two single-file versions both present with distinct version_id
    assert sorted(t["version_id"] for t in sync_titles) == sorted([v1["id"], v2["id"]])
    assert v_mp["id"] not in [t["version_id"] for t in sync_titles]

    k1 = await hayaa.content_key_for(str(f1), version_id=v1["id"])
    k2 = await hayaa.content_key_for(str(f2), version_id=v2["id"])
    assert k1 != k2

    # Different segments per version -> independent push/pull/apply without cross-talk
    await db.insert_segment("g-dual", "Dual Film", 10_000, 15_000, labels="BUTTOCKS_EXPOSED", severity="medium", version_id=v1["id"])
    await db.set_setting("hayaa_api_url", "https://api.test")
    await db.set_setting("hayaa_auth_url", "https://auth.test")
    hayaa._write_private(hayaa._token_path(), b"ref")

    pushed = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/changes":
            return httpx.Response(200, json={"titles": [], "cursor": "c1", "more": False})
        body = json.loads(req.content)
        if req.url.path == "/api/v1/push":
            pushed.extend(body.get("observations", []))
            return httpx.Response(202, json={"batch": "b1", "queued": len(body.get("observations", []))})
        if req.url.path == "/api/v1/media":
            return httpx.Response(202, json={"batch": "b2", "queued": len(body.get("media", []))})
        if req.url.path == "/api/v1/pull":
            views = {
                k2: {
                    "version": "v1",
                    "complete": True,
                    "claims": [{
                        "claim_id": "claim_4k",
                        "start": 40.0,
                        "end": 45.0,
                        "status": "accepted",
                        "basis": "human",
                        "category": "nudity",
                        "severity": "high",
                        "machine": {},
                        "supporters": 2,
                        "rejecters": 0,
                    }],
                },
            }
            return httpx.Response(200, json={"views": views, "deferred": {}, "challenges": {}})
        return httpx.Response(404)

    hayaa._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await hs.run_sync(manual=True)

    by_title = {o["title"]: o["kind"] for o in pushed}
    assert by_title[k1] == "detect"  # 1080p had local detection
    assert by_title[k2] == "clean"   # 4K had no local detection

    # Pulled claim on 4K applies ONLY to 4K, leaving 1080p segments intact!
    s1 = await db.get_segments_for_version(v1["id"])
    s2 = await db.get_segments_for_version(v2["id"])
    assert len(s1) == 1 and s1[0]["source"] == "scanner"
    assert len(s2) == 1 and s2[0]["source"] == "hayaa" and s2[0]["start_ms"] == 40_000


# ── HTTP API version scoping ──────────────────────────────────────────────────

async def test_api_lists_versions_and_scopes_segment_and_scan_actions(http_client):
    await db.upsert_scan_jobs_batch([{
        "plex_guid": "g1", "title": "Dual Film", "file_path": "/m/1080.mkv", "rating_key": "9",
        "library_id": "1", "library_title": "Movies",
        "versions": [
            {"plex_media_id": "m1", "resolution": "1080", "file_path": "/m/1080.mkv", "duration_ms": 100000},
            {"plex_media_id": "m2", "resolution": "4k", "file_path": "/m/4k.mkv", "duration_ms": 100000},
        ],
    }])
    v1, v2 = await db.get_title_versions("g1")
    await db.update_version_status(v1["id"], "done", progress=1.0)
    await db.update_version_status(v2["id"], "pending")
    await db.insert_segment("g1", "Dual Film", 10_000, 15_000, version_id=v1["id"])
    await db.insert_segment("g1", "Dual Film", 20_000, 25_000, version_id=v2["id"])

    # Library listing returns title once with both versions inside `versions`
    titles_resp = (await http_client.get("/api/libraries/1/titles")).json()
    assert len(titles_resp["titles"]) == 1
    title_obj = titles_resp["titles"][0]
    assert len(title_obj["versions"]) == 2
    assert [v["resolution"] for v in title_obj["versions"]] == ["1080", "4k"]
    assert title_obj["versions"][0]["segment_count"] == 1
    assert title_obj["versions"][0]["status"] == "done"
    assert title_obj["versions"][1]["status"] == "pending"

    # GET segments scoped to one version
    segs_v1 = (await http_client.get(f"/api/titles/g1/segments?version_id={v1['id']}")).json()["segments"]
    assert len(segs_v1) == 1 and segs_v1[0]["start_ms"] == 10_000

    # DELETE segments scoped to v1 leaves v2 segments untouched
    del_resp = await http_client.delete(f"/api/titles/g1/segments?version_id={v1['id']}")
    assert del_resp.status_code == 200 and del_resp.json()["deleted"] == 1
    assert await db.get_segments_for_version(v1["id"]) == []
    assert len(await db.get_segments_for_version(v2["id"])) == 1

    # Queueing scan for just v1 leaves v2 untouched
    with patch("cleanplex.web.routes.scanner_routes.scan_mod.enqueue_versions", new=AsyncMock()) as enq:
        scan_resp = await http_client.post("/api/scan/title", json={"plex_guid": "g1", "version_id": v1["id"]})
    assert scan_resp.status_code == 200
    enq.assert_awaited_once_with([v1["id"]])
