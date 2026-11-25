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
    await db.put_hayaa_content_key("/m/a.mkv", 10, 1, "k" * 64)
    assert await db.get_hayaa_content_key("/m/a.mkv", 10, 1) == "k" * 64
    assert await db.get_hayaa_content_key("/m/a.mkv", 11, 1) is None
    assert await db.get_hayaa_content_key("/m/missing.mkv", 10, 1) is None


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
