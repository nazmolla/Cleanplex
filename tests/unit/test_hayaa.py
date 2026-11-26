"""Hayaa client (#221): protocol pinned to Hayaa's own outputs, content-key cache, device flow."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cleanplex import database as db
from cleanplex import hayaa

pytestmark = pytest.mark.usefixtures("setup_db")

# Produced by the Hayaa repo's hayaa.protocol for the same inputs (2026-11-23).
BIG = bytes((i * 7 + 3) % 256 for i in range(2_000_000))
CK_BIG = "947d5f56ded6a992dc879904fff02ff0e95f011eced1d78114654953b76db4e6"
CK_SMALL = "37b974425848859b55a7c7cc475504b218c9afb0e6494489dcef7b26162da347"
RK = "7e2abf2b0e710c1ffaf420aad71c7b11587411d587e5b5265aa16f5f7c581f31"
KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
KID = "A6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg"
SIG = "lXiqgwWnR22nNaJJ_JFOwexOasocgsaXy8qILREKGnjy-i0gGepcVX6d_cB2mukSN6QVDWP45fJC-iLlj8MpAg"


@pytest.fixture(autouse=True)
def reset_state():
    hayaa._access, hayaa._link, hayaa._link_task = None, None, None
    yield
    if hayaa._link_task is not None:
        hayaa._link_task.cancel()
    hayaa._access, hayaa._link, hayaa._link_task, hayaa._http = None, None, None, None


def test_content_key_matches_hayaa_for_large_and_small_files():
    assert hayaa.content_key(len(BIG), lambda o, n: BIG[o:o + n]) == CK_BIG
    small = b"hello hayaa"
    assert hayaa.content_key(len(small), lambda o, n: small[o:o + n]) == CK_SMALL


def test_release_key_matches_hayaa_and_ignores_the_folder():
    assert hayaa.release_key("Movies/The.Film.2001.1080p.mkv", 123456789) == RK
    assert hayaa.release_key("D:\\other\\THE.FILM.2001.1080P.MKV", 123456789) == RK


def test_signature_matches_hayaa():
    h = hayaa.sign_headers(KEY, "POST", "/api/v1/push", b"{}", now=1700000000, nonce="abcdefghijklmnopqrstuv")
    assert h == {"X-HY-Key": KID, "X-HY-Timestamp": "1700000000",
                 "X-HY-Nonce": "abcdefghijklmnopqrstuv", "X-HY-Signature": SIG}


def test_content_key_rejects_an_empty_file():
    with pytest.raises(ValueError):
        hayaa.content_key(0, lambda o, n: b"")


async def test_content_key_is_cached_until_the_file_changes(tmp_path, monkeypatch):
    f = tmp_path / "movie.mkv"
    f.write_bytes(BIG)
    await db.upsert_scan_jobs_batch([{"plex_guid": "g1", "title": "Movie", "file_path": str(f),
                                      "rating_key": "1", "library_id": "1", "library_title": "Movies"}])
    calls = []
    real = hayaa.content_key_for_file
    monkeypatch.setattr(hayaa, "content_key_for_file", lambda p: calls.append(p) or real(p))
    assert await hayaa.content_key_for(str(f)) == CK_BIG
    assert await hayaa.content_key_for(str(f)) == CK_BIG
    assert len(calls) == 1
    f.write_bytes(b"hello hayaa")
    assert await hayaa.content_key_for(str(f)) == CK_SMALL
    assert len(calls) == 2


async def test_cached_content_key_misses_for_a_changed_or_unknown_file():
    await db.upsert_scan_jobs_batch([{"plex_guid": "g1", "title": "A", "file_path": "/m/a.mkv",
                                      "rating_key": "1", "library_id": "1", "library_title": "Movies"}])
    await db.set_file_content_key("/m/a.mkv", 10, 1, "k" * 64)
    assert await db.get_file_content_key("/m/a.mkv", 10, 1) == "k" * 64
    assert await db.get_file_content_key("/m/a.mkv", 11, 1) is None
    assert await db.get_file_content_key("/m/missing.mkv", 10, 1) is None


async def test_migration_moves_legacy_hayaa_content_keys_without_rehashing(tmp_path, monkeypatch):
    """Existing hayaa_content_keys rows move onto scan_jobs and removed_titles and the table is dropped (#235)."""
    live_file = tmp_path / "live.mkv"
    live_file.write_bytes(BIG)
    st = live_file.stat()
    db_file = tmp_path / "legacy.db"
    import aiosqlite
    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(f"""
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE scan_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, plex_guid TEXT UNIQUE NOT NULL,
                title TEXT, file_path TEXT, part_files TEXT DEFAULT '', rating_key TEXT,
                library_id TEXT, library_title TEXT, status TEXT DEFAULT 'pending',
                progress REAL DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE removed_titles (
                plex_guid TEXT PRIMARY KEY, title TEXT, rating_key TEXT, file_path TEXT,
                part_files TEXT DEFAULT '', removed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, purged_at TIMESTAMP
            );
            CREATE TABLE hayaa_content_keys (
                file_path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime INTEGER NOT NULL,
                content_key TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO scan_jobs(plex_guid, title, file_path, rating_key, library_id, library_title)
                VALUES ('g1', 'Live', '{live_file}', '1', '1', 'Movies');
            INSERT INTO removed_titles(plex_guid, title, file_path)
                VALUES ('g2', 'Gone', '/m/gone.mkv');
            INSERT INTO hayaa_content_keys(file_path, size, mtime, content_key)
                VALUES ('{live_file}', {st.st_size}, {int(st.st_mtime)}, '{CK_BIG}'),
                       ('/m/gone.mkv', 789, 101, '{"b" * 64}');
        """)
        await conn.commit()

    old_path = db.get_db_path()
    try:
        db.set_db_path(db_file)
        await db.init_db()
        monkeypatch.setattr(hayaa, "content_key_for_file", lambda _p: (_ for _ in ()).throw(AssertionError("rehashed")))
        assert await hayaa.content_key_for(str(live_file)) == CK_BIG
        removed = await db.get_hayaa_removed_titles()
        assert len(removed) == 1 and removed[0]["content_key"] == "b" * 64 and removed[0]["size"] == 789
        async with db.get_connection() as conn:
            row = await (await conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hayaa_content_keys'"
            )).fetchone()
        assert row is None
    finally:
        db.set_db_path(old_path)


def test_key_is_created_once_with_private_permissions():
    key = hayaa.load_or_create_key()
    assert hayaa.key_id(hayaa.load_or_create_key()) == hayaa.key_id(key)
    assert stat.S_IMODE(os.stat(hayaa._key_path()).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(hayaa.state_dir()).st_mode) == 0o700


def test_key_lives_beside_the_database_not_in_the_home_dir():
    assert hayaa.state_dir() == db.get_db_path().parent / "hayaa"


@pytest.mark.parametrize("url", ["http://hayaa.example", "ftp://x", "https//x"])
def test_urls_must_be_https(url):
    with pytest.raises(hayaa.HayaaError):
        hayaa._check_url(url, "service")


def test_plain_http_is_allowed_to_this_machine_for_the_dev_server():
    assert hayaa._check_url("http://127.0.0.1:8787/", "service") == "http://127.0.0.1:8787"


class FakeHayaa:
    """Just enough of Hayaa's auth and key endpoints to drive the device flow."""

    def __init__(self, approve_after=1, token_error=None):
        self.polls, self.approve_after, self.token_error = 0, approve_after, token_error
        self.keys, self.revoked, self.refreshes = [], [], 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()} if req.method == "POST" else {}
        if path == "/oauth/device_authorization":
            assert form["client_id"] == "cleanplex"
            return httpx.Response(200, json={"device_code": "dev-secret", "user_code": "WXYZ-1234",
                                             "verification_uri": "https://auth.test/link",
                                             "verification_uri_complete": "https://auth.test/link?code=WXYZ-1234",
                                             "expires_in": 900, "interval": 0})
        if path == "/oauth/token" and form.get("grant_type") == hayaa.DEVICE_GRANT:
            assert form["device_code"] == "dev-secret"
            self.polls += 1
            if self.token_error:
                return httpx.Response(400, json={"error": self.token_error})
            if self.polls <= self.approve_after:
                return httpx.Response(400, json={"error": "authorization_pending"})
            return httpx.Response(200, json={"access_token": "acc1", "refresh_token": "ref1", "expires_in": 900})
        if path == "/oauth/token" and form.get("grant_type") == "refresh_token":
            self.refreshes += 1
            if form["refresh_token"] != f"ref{self.refreshes}":
                return httpx.Response(400, json={"error": "invalid_grant"})
            n = self.refreshes + 1
            return httpx.Response(200, json={"access_token": f"acc{n}", "refresh_token": f"ref{n}", "expires_in": 900})
        if path == "/oauth/revoke":
            self.revoked.append(form["token"])
            return httpx.Response(200, json={})
        if path == "/api/v1/keys/add":
            assert req.headers["authorization"].startswith("Bearer acc")
            self.keys.append(json.loads(req.content))
            return httpx.Response(201, json={"key": self.keys[-1]["public_key"]})
        return httpx.Response(404, json={"error": "not found"})


async def _use(fake: FakeHayaa):
    await db.set_setting("hayaa_api_url", "https://api.test")
    await db.set_setting("hayaa_auth_url", "https://auth.test")
    hayaa._http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))


async def test_link_shows_a_code_then_stores_tokens_and_registers_the_key():
    fake = FakeHayaa(approve_after=2)
    await _use(fake)
    shown = await hayaa.start_link()
    assert shown["user_code"] == "WXYZ-1234" and shown["status"] == "pending"
    assert "device_code" not in shown
    await hayaa._link_task
    st = await hayaa.status()
    assert st["linked"] and st["link"]["status"] == "linked"
    assert fake.keys == [{"public_key": st["key_id"], "label": hayaa.device_label()}]
    assert stat.S_IMODE(os.stat(hayaa._token_path()).st_mode) == 0o600
    assert "ref1" not in json.dumps(st) and "dev-secret" not in json.dumps(st)


@pytest.mark.parametrize("error,message", [("access_denied", "declined"), ("expired_token", "expired")])
async def test_declined_or_expired_link_reports_an_error_and_stays_unlinked(error, message):
    await _use(FakeHayaa(token_error=error))
    await hayaa.start_link()
    await hayaa._link_task
    st = await hayaa.status()
    assert not st["linked"] and st["link"]["status"] == "error" and message in st["link"]["error"]


async def test_access_token_refresh_rotates_the_refresh_token():
    fake = FakeHayaa(approve_after=0)
    await _use(fake)
    await hayaa.start_link()
    await hayaa._link_task
    hayaa._access = None  # force a refresh
    assert await hayaa.access_token() == "acc2"
    assert hayaa._token_path().read_text() == "ref2"


async def test_revoked_device_becomes_unlinked_on_refresh():
    fake = FakeHayaa(approve_after=0)
    await _use(fake)
    await hayaa.start_link()
    await hayaa._link_task
    hayaa._token_path().write_text("stale")
    hayaa._access = None
    with pytest.raises(hayaa.NotLinked):
        await hayaa.access_token()
    assert not hayaa.is_linked()


async def test_unlink_revokes_and_forgets_the_token_but_keeps_the_key():
    fake = FakeHayaa(approve_after=0)
    await _use(fake)
    await hayaa.start_link()
    await hayaa._link_task
    kid = (await hayaa.status())["key_id"]
    await hayaa.unlink()
    assert fake.revoked == ["ref1"] and not hayaa.is_linked()
    assert hayaa.key_id(hayaa.load_or_create_key()) == kid


async def test_busy_service_raises_hayaa_busy_with_retry_after():
    await db.set_setting("hayaa_api_url", "https://api.test")
    await db.set_setting("hayaa_auth_url", "https://auth.test")
    hayaa._http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"Retry-After": "120"})))
    with pytest.raises(hayaa.HayaaBusy) as exc:
        await hayaa.start_link()
    assert exc.value.retry_after == 120


async def test_urls_default_to_this_builds_service_and_settings_override_them():
    assert await hayaa.get_urls() == (hayaa.DEFAULT_API_URL, hayaa.DEFAULT_AUTH_URL)
    await db.set_setting("hayaa_api_url", "https://api.test")
    await db.set_setting("hayaa_auth_url", "")
    assert await hayaa.get_urls() == ("https://api.test", hayaa.DEFAULT_AUTH_URL)


# Pinned to Hayaa's own outputs (hayaa.sync.guard.proof_answer / proof_offset).
def test_proof_answer_matches_hayaa():
    assert hayaa.proof_answer(b"hayaa") == "1e9e514dd591b3d7d06e9d4ce8265103"
    assert hayaa.proof_answer(bytes(range(256)) * 256) == "216e3407c768c2ac4973ce042b99a306"


def test_proof_offset_matches_hayaa():
    assert [hayaa.proof_offset(p, 10**9) for p in (0, 512, 1023)] == [0, 499967232, 998957965]
    assert hayaa.proof_offset(5, 1000) == 0


def test_proofs_for_file_reject_bad_positions(tmp_path):
    import pytest
    f = tmp_path / "a.bin"
    f.write_bytes(b"y" * 200_000)
    assert set(hayaa.proofs_for_file(f, [3, 1000])) == {"3", "1000"}
    with pytest.raises(ValueError):
        hayaa.proofs_for_file(f, [1024])


# ── Moving to a new Hayaa server (#237) ────────────────────────────────────────

def test_built_in_urls_point_at_the_single_hayaa_host():
    assert hayaa.DEFAULT_API_URL == "https://hayaa-dev.canadacentral.cloudapp.azure.com"
    assert hayaa.DEFAULT_AUTH_URL == hayaa.DEFAULT_API_URL


async def _state_for_old_server():
    """A linked instance that has synced with another server: ledger, claims, overrides, progress."""
    await db.upsert_scan_jobs_batch([{"plex_guid": "g1", "title": "Film", "file_path": "/m/film.mkv",
                                      "rating_key": "1", "library_id": "1", "library_title": "Movies"}])
    await db.set_file_content_key("/m/film.mkv", 10, 20, "ck1")
    seg = await db.insert_segment("g1", "Film", 1_000, 2_000, labels="BUTTOCKS_EXPOSED", severity="medium")
    async with db.get_connection() as conn:
        await conn.execute("INSERT INTO hayaa_titles(title_key, plex_guid) VALUES ('ck1', 'g1')")
        await conn.execute("INSERT INTO hayaa_sent(obs_id, base_id, title_key, kind) VALUES ('o1', 'b1', 'ck1', 'seg')")
        await conn.execute("INSERT INTO hayaa_claims(title_key, claim_id, plex_guid, start_ms, end_ms, status, basis) "
                           "VALUES ('ck1', 'c1', 'g1', 0, 1, 'accepted', 'consensus')")
        await conn.execute("INSERT INTO hayaa_overrides(title_key, claim_id, verdict) VALUES ('ck1', 'c1', 'delete')")
        await conn.execute("UPDATE title_versions SET hayaa_sync_state='synced'")
        await conn.commit()
    for k in ("hayaa_feed_cursor", "hayaa_partial", "hayaa_deferred", "hayaa_quota",
              "hayaa_paused_until", "hayaa_last_result", "hayaa_last_sync"):
        await db.set_setting(k, "old")
    await db.set_setting("hayaa_server", "https://old.example")
    hayaa.load_or_create_key()
    hayaa._write_private(hayaa._token_path(), b"refresh")
    return seg


async def _count(table):
    async with db.get_connection() as conn:
        return (await (await conn.execute(f"SELECT COUNT(*) FROM {table}")).fetchone())[0]


async def test_a_new_server_starts_the_hayaa_state_over_and_keeps_keys_and_segments():
    seg = await _state_for_old_server()
    old_key = hayaa._key_path().read_bytes()

    assert await hayaa.reset_for_server_change() is True

    for table in ("hayaa_sent", "hayaa_claims", "hayaa_titles", "hayaa_overrides"):
        assert await _count(table) == 0, table
    settings = await db.get_all_settings()
    for k in ("hayaa_feed_cursor", "hayaa_partial", "hayaa_deferred", "hayaa_quota",
              "hayaa_paused_until", "hayaa_last_result", "hayaa_last_sync"):
        assert k not in settings, k
    assert settings["hayaa_server"] == hayaa.DEFAULT_API_URL
    async with db.get_connection() as conn:
        states = {r[0] for r in await conn.execute_fetchall("SELECT hayaa_sync_state FROM title_versions")}
    assert states == {"pending"}
    assert not hayaa.is_linked() and not hayaa._key_path().exists()
    assert hayaa.load_or_create_key().private_bytes_raw() != old_key  # a new identity for the new server
    assert await db.get_file_content_key("/m/film.mkv", 10, 20) == "ck1"
    assert seg in {s["id"] for s in await db.get_segments_for_guid("g1")}


async def test_the_same_server_again_changes_nothing():
    await _state_for_old_server()
    await db.set_setting("hayaa_server", hayaa.DEFAULT_API_URL)

    assert await hayaa.reset_for_server_change() is False

    assert await _count("hayaa_sent") == 1 and hayaa.is_linked()


async def test_a_settings_override_is_the_server_that_counts():
    await _state_for_old_server()
    await db.set_setting("hayaa_api_url", "https://old.example")

    assert await hayaa.reset_for_server_change() is False
