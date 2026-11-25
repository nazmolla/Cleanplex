"""Hayaa client (#221): links this instance to the Hayaa segment service and identifies files.

Linking uses the OAuth device flow: Cleanplex shows a short code, the user approves it on the
Hayaa site, and Cleanplex then registers an Ed25519 instance key that signs every sync request.
The private key and the refresh token live in ``<data dir>/hayaa/`` with 0600 permissions,
outside the database, so database backups never contain them and no API returns them.

Request signing, content keys and release keys follow ``hayaa.protocol`` in the Hayaa repo. That
repo is private and its client is blocking, so they are re-implemented here and pinned to its
own outputs by test vectors (tests/unit/test_hayaa.py).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import socket
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import database as db
from .logger import get_logger

logger = get_logger(__name__)

CLIENT_ID = "cleanplex"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
HTTP_TIMEOUT_S = 30.0
# Renew the 15-minute access token a minute early so a request never carries an expired one.
ACCESS_TOKEN_MARGIN_S = 60
# Hayaa answers slow_down when polled faster than every 5 s; RFC 8628 says add 5 s each time.
SLOW_DOWN_STEP_S = 5

CHUNK = 64 * 1024
N_CHUNKS = 16
CONTENT_KEY_VERSION = b"cpk1"

_LABEL_CHARS = re.compile(r"[^A-Za-z0-9 _-]")


class HayaaError(RuntimeError):
    """A Hayaa request failed; the message is safe to show the user."""


class NotLinked(HayaaError):
    """This instance is not (or no longer) linked to a Hayaa account."""


class HayaaBusy(HayaaError):
    """Quota or flood breaker (429/503): stop and try later, never retry in a loop."""

    def __init__(self, retry_after: int | None) -> None:
        super().__init__("Hayaa is busy; try again later")
        self.retry_after = retry_after


# ── Protocol (mirrors hayaa.protocol) ──────────────────────────────────────────

def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def canonical_string(method: str, path: str, ts: str, nonce: str, body: bytes) -> bytes:
    """The bytes an instance signs: method, path, time, nonce and the body's SHA-256."""
    return "\n".join(["HAYAA1", method.upper(), path, ts, nonce, hashlib.sha256(body).hexdigest()]).encode()


def public_raw(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def key_id(key: Ed25519PrivateKey) -> str:
    """The public key as Hayaa names it (base64url of the raw 32 bytes)."""
    return b64e(public_raw(key))


def contributor_id(key: Ed25519PrivateKey) -> str:
    return hashlib.sha256(public_raw(key)).hexdigest()[:32]


def sign_headers(key: Ed25519PrivateKey, method: str, path: str, body: bytes,
                 now: float | None = None, nonce: str | None = None) -> dict[str, str]:
    """Headers that authenticate one request with the instance key."""
    ts = str(int(now if now is not None else time.time()))
    nonce = nonce or secrets.token_urlsafe(18)
    sig = key.sign(canonical_string(method, path, ts, nonce, body))
    return {"X-HY-Key": key_id(key), "X-HY-Timestamp": ts, "X-HY-Nonce": nonce, "X-HY-Signature": b64e(sig)}


def content_key(size: int, read: Callable[[int, int], bytes]) -> str:
    """Hayaa's file identity: SHA-256 over the size and 16 evenly spaced 64 KB chunks.

    Sampling instead of hashing the whole file reads about 1 MB per file, and a player that
    only streams the file can compute the same key with range requests.
    """
    if size <= 0:
        raise ValueError("empty file")
    h = hashlib.sha256(CONTENT_KEY_VERSION + size.to_bytes(8, "big"))
    if size <= CHUNK * N_CHUNKS:
        spans = [(0, size)]
    else:
        spans = [((i * (size - CHUNK)) // (N_CHUNKS - 1), CHUNK) for i in range(N_CHUNKS)]
    for offset, length in spans:
        data = read(offset, length)
        if len(data) != length:
            raise ValueError("short read")
        h.update(data)
    return h.hexdigest()


def content_key_for_file(path: Path) -> str:
    """Blocking: the content key of a local file; call via asyncio.to_thread."""
    size = path.stat().st_size
    with path.open("rb") as f:
        def read(offset: int, length: int) -> bytes:
            f.seek(offset)
            return f.read(length)

        return content_key(size, read)


PROOF_POSITIONS = 1024  # Hayaa's AbuseConfig.proof_positions


def proof_answer(chunk: bytes) -> str:
    """Answer to one possession challenge (ABUSE_RESISTANCE §2): 32 hex of SHA-256 of the chunk."""
    return hashlib.sha256(b"hayaa-proof:" + chunk).hexdigest()[:32]


def proof_offset(position: int, size: int) -> int:
    """Byte offset of a challenged position, as Hayaa computes it."""
    return max(0, size - CHUNK) * position // PROOF_POSITIONS


def proofs_for_file(path: Path, positions: list[int]) -> dict[str, str]:
    """Blocking: answers for the challenged positions of a local file; call via asyncio.to_thread."""
    size = path.stat().st_size
    out: dict[str, str] = {}
    with path.open("rb") as f:
        for pos in positions:
            if not 0 <= int(pos) < PROOF_POSITIONS:
                raise ValueError("challenge position out of range")
            f.seek(proof_offset(int(pos), size))
            out[str(int(pos))] = proof_answer(f.read(CHUNK))
    return out


def release_key(file_name: str, size: int) -> str:
    """Hash of the file name (folder dropped, lowercased) and exact size; the name is never sent."""
    if size <= 0:
        raise ValueError("size must be positive")
    base = file_name.replace("\\", "/").rsplit("/", 1)[-1]
    base = unicodedata.normalize("NFC", base).strip().lower()
    if not base:
        raise ValueError("empty file name")
    return hashlib.sha256(f"cprk1|{base}|{size}".encode()).hexdigest()


async def content_key_for(file_path: str) -> str:
    """Content key of a media file, cached by (path, size, mtime) in the database."""
    st = await asyncio.to_thread(os.stat, file_path)
    size, mtime = st.st_size, int(st.st_mtime)
    cached = await db.get_hayaa_content_key(file_path, size, mtime)
    if cached:
        return cached
    key = await asyncio.to_thread(content_key_for_file, Path(file_path))
    await db.put_hayaa_content_key(file_path, size, mtime, key)
    return key


# ── Local state: instance key and refresh token ────────────────────────────────

def state_dir() -> Path:
    """``<data dir>/hayaa``: beside the database, so a dev instance never shares production's key."""
    return db.get_db_path().parent / "hayaa"


def _key_path() -> Path:
    return state_dir() / "sync.key"


def _token_path() -> Path:
    return state_dir() / f"{CLIENT_ID}.token"


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def load_or_create_key() -> Ed25519PrivateKey:
    """The instance's signing key, created on first use (32 bytes, so reading it inline is fine)."""
    path = _key_path()
    if path.exists():
        return Ed25519PrivateKey.from_private_bytes(path.read_bytes())
    key = Ed25519PrivateKey.generate()
    _write_private(path, key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption(),
    ))
    return key


def is_linked() -> bool:
    return _token_path().exists()


def device_label() -> str:
    """How the instance is listed on the Hayaa account page (Hayaa allows 40 safe characters)."""
    host = _LABEL_CHARS.sub("", socket.gethostname().split(".")[0])
    return f"Cleanplex {host}".strip()[:40]


# ── Service URLs and HTTP ──────────────────────────────────────────────────────

def _check_url(url: str, what: str) -> str:
    """https only, except plain http to this machine for Hayaa's local dev server."""
    url = url.strip().rstrip("/")
    parsed = urlparse(url)
    loopback = parsed.hostname in ("127.0.0.1", "localhost", "::1")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise HayaaError(f"The Hayaa {what} URL must use https")
    return url


# The Hayaa service this build talks to. Not shown in the UI; the hayaa_api_url / hayaa_auth_url
# settings still override them (a local Hayaa dev server, for instance).
DEFAULT_API_URL = "https://hayaa-api-oxc7hijrqqqmk.azurewebsites.net"
DEFAULT_AUTH_URL = "https://hayaa-auth-oxc7hijrqqqmk.azurewebsites.net"


def urls_from(settings: dict[str, str]) -> tuple[str, str]:
    """(api_url, auth_url): the saved settings, else this build's defaults."""
    return ((settings.get("hayaa_api_url") or "").strip() or DEFAULT_API_URL,
            (settings.get("hayaa_auth_url") or "").strip() or DEFAULT_AUTH_URL)


async def get_urls() -> tuple[str, str]:
    """(api_url, auth_url) from settings or the defaults; raises HayaaError when not https."""
    api, auth = urls_from(await db.get_all_settings())
    return _check_url(api, "service"), _check_url(auth, "sign-in")


_http: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    # One client for the process (§5.3), created on first use; close() runs at shutdown.
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=HTTP_TIMEOUT_S)
    return _http


async def close() -> None:
    global _http
    if _http is not None:
        await _http.aclose()
        _http = None


def _json(resp: httpx.Response) -> Any:
    if resp.status_code in (429, 503):
        ra = resp.headers.get("retry-after", "")
        raise HayaaBusy(int(ra) if ra.isdigit() else None)
    try:
        return resp.json() if resp.content else None
    except ValueError:
        raise HayaaError(f"Hayaa sent an unreadable response ({resp.status_code})") from None


async def _oauth(path: str, form: dict[str, str]) -> tuple[int, Any]:
    _, auth = await get_urls()
    try:
        resp = await _client().post(auth + path, data=form)
    except httpx.HTTPError as exc:
        raise HayaaError(f"Could not reach Hayaa: {type(exc).__name__}") from exc
    return resp.status_code, _json(resp)


# ── Tokens ─────────────────────────────────────────────────────────────────────

# Event-loop-only state: the current access token and its expiry (monotonic seconds).
_access: tuple[str, float] | None = None


def _store_tokens(t: dict[str, Any]) -> str:
    global _access
    _write_private(_token_path(), t["refresh_token"].encode())
    _access = (t["access_token"], time.monotonic() + int(t.get("expires_in", 900)) - ACCESS_TOKEN_MARGIN_S)
    return t["access_token"]


async def access_token() -> str:
    """A valid access token, refreshing (and rotating the refresh token) when needed."""
    global _access
    if _access and _access[1] > time.monotonic():
        return _access[0]
    if not is_linked():
        raise NotLinked("Not signed in to Hayaa")
    refresh = (await asyncio.to_thread(_token_path().read_text)).strip()
    status, t = await _oauth("/oauth/token", {
        "grant_type": "refresh_token", "refresh_token": refresh, "client_id": CLIENT_ID,
    })
    if status != 200:
        if isinstance(t, dict) and t.get("error") == "invalid_grant":
            # Unlinked on the Hayaa site, or a reused token revoked the device.
            _token_path().unlink(missing_ok=True)
            _access = None
            raise NotLinked("This instance was unlinked from Hayaa; sign in again")
        raise HayaaError(f"Token refresh failed ({status})")
    # The old refresh token is dead from here on, so it is replaced before anything else.
    return _store_tokens(t)


async def register_key() -> None:
    """Register the instance key with the signed-in account (idempotent on Hayaa's side)."""
    api, _ = await get_urls()
    key = await asyncio.to_thread(load_or_create_key)
    token = await access_token()
    try:
        resp = await _client().post(
            api + "/api/v1/keys/add",
            json={"public_key": key_id(key), "label": device_label()},
            headers={"Authorization": f"Bearer {token}"},
        )
    except httpx.HTTPError as exc:
        raise HayaaError(f"Could not reach Hayaa: {type(exc).__name__}") from exc
    body = _json(resp)
    if resp.status_code not in (200, 201):
        err = body.get("error") if isinstance(body, dict) else None
        raise HayaaError(f"Hayaa refused this instance's key: {err or resp.status_code}")


# ── Linking (device flow) ──────────────────────────────────────────────────────

# Event-loop-only state of the current or last link attempt. device_code stays here and is
# never returned: it is what the poll exchanges for tokens.
_link: dict[str, Any] | None = None
_link_task: asyncio.Task | None = None


def _public_link() -> dict[str, Any] | None:
    if _link is None:
        return None
    return {k: _link[k] for k in ("status", "user_code", "verification_uri", "expires_in_s", "error")}


async def start_link() -> dict[str, Any]:
    """Ask Hayaa for a device code and start polling in the background; returns what to show."""
    global _link, _link_task
    if _link_task is not None and not _link_task.done():
        return _public_link() or {}
    status, d = await _oauth("/oauth/device_authorization", {"client_id": CLIENT_ID})
    if status != 200 or not isinstance(d, dict):
        err = d.get("error") if isinstance(d, dict) else None
        raise HayaaError(f"Could not start signing in: {err or status}")
    expires_at = time.monotonic() + int(d["expires_in"])
    _link = {
        "status": "pending",
        "user_code": d["user_code"],
        "verification_uri": d.get("verification_uri_complete") or d["verification_uri"],
        "expires_at": expires_at,
        "expires_in_s": int(d["expires_in"]),
        "error": "",
        "device_code": d["device_code"],
    }
    _link_task = asyncio.create_task(_poll_link(d["device_code"], int(d.get("interval", 5)), expires_at))
    return _public_link() or {}


def _finish_link(status: str, error: str = "") -> None:
    if _link is not None:
        _link.update(status=status, error=error, device_code="")


async def _poll_link(device_code: str, interval: int, expires_at: float) -> None:
    """Poll the token endpoint until approved, declined or expired (the deadline bounds the loop)."""
    try:
        while time.monotonic() < expires_at:
            await asyncio.sleep(interval)
            status, t = await _oauth("/oauth/token", {
                "grant_type": DEVICE_GRANT, "device_code": device_code, "client_id": CLIENT_ID,
            })
            if status == 200 and isinstance(t, dict):
                _store_tokens(t)
                await register_key()
                _finish_link("linked")
                logger.info("Linked to Hayaa as %s", device_label())
                return
            err = t.get("error") if isinstance(t, dict) else None
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += SLOW_DOWN_STEP_S
                continue
            messages = {"access_denied": "Sign-in was declined", "expired_token": "The code expired; try again"}
            _finish_link("error", messages.get(str(err), f"Sign-in failed: {err or status}"))
            return
        _finish_link("error", "The code expired; try again")
    except HayaaError as exc:
        _finish_link("error", str(exc))
    except Exception as exc:  # the task is fire-and-forget; an unexpected error must still show up
        logger.exception("Hayaa sign-in failed")
        _finish_link("error", f"Sign-in failed: {type(exc).__name__}")


async def unlink() -> None:
    """Revoke this instance on Hayaa (best effort) and forget the refresh token; the key stays."""
    global _access, _link, _link_task
    if _link_task is not None and not _link_task.done():
        _link_task.cancel()
    _link_task, _link = None, None
    if is_linked():
        refresh = (await asyncio.to_thread(_token_path().read_text)).strip()
        try:
            await _oauth("/oauth/revoke", {"token": refresh, "client_id": CLIENT_ID})
        except HayaaError as exc:
            # Removing the token locally still unlinks this side; the account page can revoke the rest.
            logger.warning("Hayaa revoke failed: %s", exc)
        _token_path().unlink(missing_ok=True)
    _access = None


async def signed_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST a JSON body to the sync API signed with the instance key; returns the JSON reply.

    Raises HayaaBusy on 429/503, NotLinked when the key is unknown or revoked, HayaaError otherwise.
    """
    api, _ = await get_urls()
    if not is_linked():
        raise NotLinked("Not signed in to Hayaa")
    key = await asyncio.to_thread(load_or_create_key)
    raw = json.dumps(body, separators=(",", ":")).encode()
    headers = sign_headers(key, "POST", path, raw)
    headers["Content-Type"] = "application/json"
    try:
        resp = await _client().post(api + path, content=raw, headers=headers)
    except httpx.HTTPError as exc:
        raise HayaaError(f"Could not reach Hayaa: {type(exc).__name__}") from exc
    data = _json(resp)
    err = data.get("error") if isinstance(data, dict) else None
    if resp.status_code in (401, 403):
        raise NotLinked(f"Hayaa does not accept this instance ({err or resp.status_code}); sign in again")
    if resp.status_code >= 400 or not isinstance(data, dict):
        raise HayaaError(f"Hayaa refused the request: {err or resp.status_code}")
    return data


async def status() -> dict[str, Any]:
    """Linked state for the UI: never contains the private key, tokens or the device code."""
    settings = await db.get_all_settings()
    linked = is_linked()
    key = await asyncio.to_thread(load_or_create_key) if linked else None
    link = _public_link()
    if link and _link is not None and link["status"] == "pending":
        link["expires_in_s"] = max(0, int(_link["expires_at"] - time.monotonic()))
    return {
        "api_url": urls_from(settings)[0],
        "auth_url": urls_from(settings)[1],
        "configured": True,
        "linked": linked,
        "device_label": device_label(),
        "key_id": key_id(key) if key else "",
        "link": link,
    }
