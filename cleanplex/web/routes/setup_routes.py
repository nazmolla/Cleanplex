"""First-run setup (#245): find Plex on this machine, or sign in with Plex, and mark setup done.

The wizard in the web UI drives this. Connecting saves `plex_url` / `plex_token` exactly as
Settings does, so everything after setup behaves the same however Plex was connected.

Sign in with Plex uses plex.tv's PIN flow (the one Plex's own apps use): the UI opens
`auth_url`, the user approves there, and polling the PIN returns an account token. That token
is only used to find the user's own server and its access token; the account token itself is
never stored.
"""

from __future__ import annotations

import platform
import plistlib
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException

import cleanplex.plex_client as plex_mod
from ... import database as db
from ... import events
from ...frontdoor import find_plex_paths
from ...logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/setup", tags=["setup"])

PRODUCT = "Hayaa for Plex"
PLEX_TV = "https://plex.tv/api/v2"
LOCAL_URL = "http://127.0.0.1:32400"
_TIMEOUT = httpx.Timeout(15.0)


async def _client_id() -> str:
    """A stable id for this install, which plex.tv shows next to the sign-in request."""
    cid = await db.get_setting("plex_client_id")
    if not cid:
        cid = uuid.uuid4().hex
        await db.set_setting("plex_client_id", cid)
    return cid


async def _headers(token: str = "") -> dict[str, str]:
    h = {"Accept": "application/json", "X-Plex-Product": PRODUCT,
         "X-Plex-Client-Identifier": await _client_id()}
    if token:
        h["X-Plex-Token"] = token
    return h


def _local_token() -> str:
    """The server owner's token from this machine's Plex settings, if Hayaa can read them."""
    try:
        system = platform.system()
        if system == "Darwin":
            prefs = Path.home() / "Library/Preferences/com.plexapp.plexmediaserver.plist"
            with open(prefs, "rb") as fh:
                return str(plistlib.load(fh).get("PlexOnlineToken", ""))
        if system == "Windows":
            import winreg  # type: ignore[import-not-found]  # only exists on Windows
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Plex, Inc.\Plex Media Server") as key:
                value, _ = winreg.QueryValueEx(key, "PlexOnlineToken")
                return str(value)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return ""
    found = find_plex_paths()
    return found.token if found else ""


async def _try(url: str, token: str) -> tuple[bool, str]:
    client = plex_mod.PlexClient(url, token)
    try:
        return await client.test_connection()
    finally:
        await client.close()


async def _save(url: str, token: str, name: str) -> dict[str, Any]:
    await db.update_settings({"plex_url": url, "plex_token": token})
    plex_mod.init_client(url, token)
    events.publish("settings_changed")
    logger.info("Setup connected to Plex server %r at %s", name, url)
    return {"ok": True, "server_name": name, "plex_url": url}


async def _setup_done() -> bool:
    flag = await db.get_setting("setup_done")
    if flag:
        return flag == "1"
    # Installs from before the wizard: anything already scanned means it was set up by hand.
    async with db.get_connection() as conn:
        row = await (await conn.execute("SELECT COUNT(*) AS n FROM scan_jobs")).fetchone()
    return bool(row and row["n"])


@router.get("/status")
async def status():
    """Whether to show the wizard, and whether Plex is connected right now."""
    settings = await db.get_all_settings()
    url, token = settings.get("plex_url", ""), settings.get("plex_token", "")
    connected, name = (await _try(url, token)) if url and token else (False, "")
    return {
        "done": await _setup_done(),
        "plex_connected": connected,
        "server_name": name if connected else "",
        "plex_url": url,
    }


@router.post("/start")
async def start():
    """The wizard opened: keep showing it until it finishes, even once Plex is connected."""
    if await db.get_setting("setup_done") != "1":
        await db.set_setting("setup_done", "0")
    return {"ok": True}


@router.post("/finish")
async def finish():
    await db.set_setting("setup_done", "1")
    events.publish("settings_changed")
    return {"ok": True}


async def _plex_answers_here() -> bool:
    """Whether a Plex server answers on this machine at all (no token needed for /identity)."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as http:
            return (await http.get(f"{LOCAL_URL}/identity")).status_code == 200
    except httpx.HTTPError:
        return False


@router.post("/detect")
async def detect():
    """Connect to Plex on this machine using the token in Plex's own settings.

    Reasons when it cannot: `not_found` (no Plex on this computer), `unreachable` (Plex's
    settings are here but the server is not answering), `sign_in_needed` (Plex answers but
    Hayaa could not read or use its token, e.g. it runs as another user, or the token is stale).
    """
    token = _local_token()
    if token:
        ok, name = await _try(LOCAL_URL, token)
        if ok:
            return await _save(LOCAL_URL, token, name)
    if await _plex_answers_here():
        return {"ok": False, "reason": "sign_in_needed"}
    return {"ok": False, "reason": "unreachable" if token else "not_found"}


@router.post("/plex/pin")
async def create_pin():
    """Start Sign in with Plex: returns the plex.tv page to open and the PIN to poll."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            r = await http.post(f"{PLEX_TV}/pins", params={"strong": "true"},
                                headers=await _headers())
            r.raise_for_status()
            pin = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Could not start Plex sign-in: %s", exc)
        raise HTTPException(502, "Could not reach plex.tv") from exc
    cid = await _client_id()
    auth_url = ("https://app.plex.tv/auth#?clientID=" + quote(cid) + "&code=" + quote(pin["code"])
                + "&context%5Bdevice%5D%5Bproduct%5D=" + quote(PRODUCT))
    return {"id": pin["id"], "auth_url": auth_url}


@router.get("/plex/pin/{pin_id}")
async def check_pin(pin_id: int):
    """Poll Sign in with Plex. Once approved, find the user's server and connect to it."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            r = await http.get(f"{PLEX_TV}/pins/{pin_id}", headers=await _headers())
            if r.status_code == 404:
                return {"status": "expired"}
            r.raise_for_status()
            account_token = r.json().get("authToken")
            if not account_token:
                return {"status": "pending"}
            r = await http.get(f"{PLEX_TV}/resources",
                               params={"includeHttps": "1", "includeRelay": "0"},
                               headers=await _headers(account_token))
            r.raise_for_status()
            resources = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Plex sign-in check failed: %s", exc)
        return {"status": "error", "reason": "plex_tv"}

    # Hayaa only works beside Plex (it reads Plex's files and filters its streams), so only the
    # owned server answering on this machine counts, never one elsewhere on the network.
    servers = [s for s in resources
               if "server" in (s.get("provides") or "") and s.get("owned") and s.get("accessToken")]
    if not servers:
        return {"status": "error", "reason": "no_server"}
    for server in servers:
        client = plex_mod.PlexClient(LOCAL_URL, server["accessToken"])
        try:
            machine = await client.get_machine_identifier()
            ok, name = await client.test_connection() if machine else (False, "")
        finally:
            await client.close()
        if ok and machine == server.get("clientIdentifier"):
            saved = await _save(LOCAL_URL, server["accessToken"], name)
            return {"status": "linked", **saved}
    return {"status": "error", "reason": "not_on_this_machine"}
