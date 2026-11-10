"""Container entrypoint (#194): configures Cleanplex from the mounted Plex folder and supervises nginx beside the app.

`python -m cleanplex.container` is the image's main process. On start it
  1. seeds the Plex URL and token (from the environment, else Plex's own Preferences.xml),
  2. runs the app (`cleanplex.main`) as a child process,
  3. keeps the nginx front door running: once Plex's certificate exists it renders the nginx
     config, starts nginx, and on the first successful start turns enforcement on and switches
     off the Plex settings that bypass the front door (Relay, DLNA).
The app or nginx dying is handled differently on purpose: nginx is restarted (a viewer must
never be left with a dead front door while the app is fine), the app ending ends the container
so the restart policy brings everything back in a known state.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import httpx

from . import database as db
from .frontdoor import FRONT_PORT, find_plex_paths, write_frontdoor
from .logger import get_logger

logger = get_logger("cleanplex.container")

DATA_DIR = Path(os.environ.get("CLEANPLEX_DATA", "/data"))
APP_PORT = int(os.environ.get("CLEANPLEX_PORT", "7979"))
APP_URL = f"http://127.0.0.1:{APP_PORT}"
# Seconds between checks while waiting for Plex to create its certificate (it only does so once
# the server is claimed) and for the app and Plex to answer after a start.
RETRY_S = 15
# Written once the first-run configuration succeeded, so a restart never overrides what the
# user later changed in the UI (enforcement off, Relay back on).
CONFIGURED_MARKER = "frontdoor/configured"
NGINX_STOP_TIMEOUT_S = 10


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return default if not raw else raw in ("1", "true", "yes", "on")


def plex_url_from_env() -> str:
    """The URL Cleanplex reaches Plex at: CLEANPLEX_PLEX_URL, else the front door's upstream address."""
    explicit = os.environ.get("CLEANPLEX_PLEX_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    return "http://" + os.environ.get("CLEANPLEX_PLEX_UPSTREAM", "127.0.0.1:32400")


async def seed_settings() -> dict[str, str]:
    """Fill in the Plex URL and token before the app starts; returns what was changed.

    An explicit CLEANPLEX_PLEX_URL / CLEANPLEX_PLEX_TOKEN always wins. Values read from Plex's
    Preferences.xml only fill a blank setting, so a token the user typed in the UI is kept.
    """
    db.set_db_path(DATA_DIR / "cleanplex.db")
    await db.init_db()
    changed: dict[str, str] = {}

    url = plex_url_from_env()
    explicit_url = bool(os.environ.get("CLEANPLEX_PLEX_URL", "").strip())
    if explicit_url or not await db.get_setting("plex_url", ""):
        if await db.get_setting("plex_url", "") != url:
            await db.set_setting("plex_url", url)
            changed["plex_url"] = url

    token = os.environ.get("CLEANPLEX_PLEX_TOKEN", "").strip()
    explicit_token = bool(token)
    if not token:
        paths = find_plex_paths()
        token = paths.token if paths else ""
    if token and (explicit_token or not await db.get_setting("plex_token", "")):
        if await db.get_setting("plex_token", "") != token:
            await db.set_setting("plex_token", token)
            changed["plex_token"] = "set"
    return changed


async def _wait_for_nginx(proc: asyncio.subprocess.Process) -> bool:
    """True once nginx accepts connections on the front port (any HTTP answer counts, even Plex's 502)."""
    async with httpx.AsyncClient(timeout=3) as client:
        for _ in range(20):
            if proc.returncode is not None:
                return False
            try:
                await client.get(f"http://127.0.0.1:{FRONT_PORT}/identity")
                return True
            except httpx.HTTPError:
                await asyncio.sleep(1)
    return False


async def _first_run_configuration(marker: Path) -> None:
    """Turn enforcement on and Relay/DLNA off through the app's own API, once.

    Retries until the app and Plex answer; gives up only when the container stops. The marker
    is written only after every step worked.
    """
    configure_plex = _flag("CLEANPLEX_CONFIGURE_PLEX", True)
    async with httpx.AsyncClient(timeout=15) as client:
        while not marker.exists():
            try:
                resp = await client.post(f"{APP_URL}/api/enforcement", json={"enabled": True})
                resp.raise_for_status()
                if configure_plex:
                    resp = await client.post(f"{APP_URL}/api/enforcement/plex-settings",
                                             json={"relay": False, "dlna": False})
                    resp.raise_for_status()
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("1")
                logger.info("Front door active: enforcement on%s", ", Plex Relay and DLNA off" if configure_plex else "")
                return
            except httpx.HTTPError as exc:
                # The app is still starting, or Plex is not claimed/reachable yet.
                logger.info("Waiting to finish front door setup (%s)", exc)
                await asyncio.sleep(RETRY_S)


async def _run_nginx(conf: Path, stop: asyncio.Event) -> int | None:
    """Run nginx in the foreground until it exits or `stop` is set; returns its exit code (None if stopped)."""
    nginx = os.environ.get("CLEANPLEX_NGINX", "nginx")
    err_log = conf.parent / "cleanplex-nginx-error.log"
    test = await asyncio.create_subprocess_exec(nginx, "-e", str(err_log), "-t", "-c", str(conf), "-q",
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await test.communicate()
    if test.returncode != 0:
        logger.error("nginx rejected its config: %s", err.decode(errors="ignore").strip())
        return test.returncode
    proc = await asyncio.create_subprocess_exec(nginx, "-e", str(err_log), "-c", str(conf), "-g", "daemon off;")
    setup = asyncio.create_task(_after_start(proc, conf))
    waiter = asyncio.create_task(proc.wait())
    stopper = asyncio.create_task(stop.wait())
    await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
    setup.cancel()
    if proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), NGINX_STOP_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
        return None
    return proc.returncode


async def _after_start(proc: asyncio.subprocess.Process, conf: Path) -> None:
    if not await _wait_for_nginx(proc):
        return
    logger.info("Front door answering on port %d", FRONT_PORT)
    await _first_run_configuration(DATA_DIR / CONFIGURED_MARKER)


async def frontdoor_supervisor(stop: asyncio.Event) -> None:
    """Keep nginx running whenever Plex's certificate is available; never returns until `stop`."""
    if not _flag("CLEANPLEX_FRONTDOOR", True):
        logger.info("Front door disabled (CLEANPLEX_FRONTDOOR=0); running without stream filtering")
        return
    waiting_logged = False
    while not stop.is_set():
        try:
            conf = await asyncio.to_thread(write_frontdoor, DATA_DIR)
        except FileNotFoundError as exc:
            if not waiting_logged:
                logger.warning("%s. Retrying every %ds; claim the Plex server and mount its config folder at "
                               "CLEANPLEX_PLEX_DATA.", exc, RETRY_S)
                waiting_logged = True
            await _sleep_or_stop(stop, RETRY_S)
            continue
        except Exception as exc:  # e.g. the certificate password no longer opens it after a re-claim
            logger.error("Could not prepare the front door: %s", exc)
            await _sleep_or_stop(stop, RETRY_S)
            continue
        waiting_logged = False
        code = await _run_nginx(conf, stop)
        if code is not None:
            logger.error("nginx exited with %s; restarting", code)
            await _sleep_or_stop(stop, 3)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except asyncio.TimeoutError:
        pass


async def _amain() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    changed = await seed_settings()
    if changed:
        logger.info("Configured from the environment: %s", ", ".join(sorted(changed)))
    app = await asyncio.create_subprocess_exec(sys.executable, "-c", "from cleanplex.main import main; main()")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _forward(sig: int) -> None:
        # The app shuts down gracefully on SIGTERM; the supervisor ends once it has.
        if app.returncode is None:
            app.send_signal(sig)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _forward, sig)
    door = asyncio.create_task(frontdoor_supervisor(stop))
    code = await app.wait()
    stop.set()
    await door
    return code


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
