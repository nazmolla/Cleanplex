"""Cleanplex entry point — starts the watcher loops and web server."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path

import uvicorn

from . import ort_runtime
from .logger import setup_logging, get_logger
from . import database as db
from .config import Config
import cleanplex.plex_client as plex_mod
from .watcher import session_watcher_loop, library_watcher_loop
from .scanner import scanner_loop
from .hover_previews import hover_preview_loop
from .segment_files import segment_cleanup_loop
from .db_backup import db_backup_loop
from .thumbnail_backfill import thumbnail_backfill_loop
from .cast_sync import cast_sync_loop
from .keyframe_index import keyframe_index_loop
from .frontdoor import frontdoor_cert_loop
from .web.app import create_app
from . import stream_proxy
from .bg_jobs import recover_stale_jobs

DATA_DIR = Path(os.environ.get("CLEANPLEX_DATA", Path.home() / ".cleanplex"))
logger = get_logger(__name__)

# Seconds uvicorn waits for open connections before dropping them. The SSE stream
# (/api/events) never ends on its own, so without a cap one open browser tab keeps the
# server, and therefore a service restart, waiting forever (#176).
GRACEFUL_SHUTDOWN_S = 5
# Seconds background loops get to unwind after the web server stops.
LOOP_CANCEL_TIMEOUT_S = 10
# Last resort: lingering ffmpeg or inference threads must not keep a stopping process alive.
HARD_EXIT_AFTER_S = 20
# Exit status for main(): 1 when a background loop died, so a supervisor that restarts only
# on failure (launchd KeepAlive SuccessfulExit=false, systemd Restart=on-failure) revives us.
_exit_code = 0


def _arm_hard_exit() -> None:
    """Kill the process HARD_EXIT_AFTER_S after shutdown starts, whatever is still running.

    A daemon timer thread, not an asyncio task: the hang being guarded against is the event
    loop or a non-daemon worker thread refusing to finish.
    """
    def _die() -> None:
        logging.shutdown()
        os._exit(_exit_code)

    timer = threading.Timer(HARD_EXIT_AFTER_S, _die)
    timer.daemon = True
    timer.start()


async def _scanner_idle() -> bool:
    """True when no scan is queued or running, which is when hover previews may render."""
    from .scanner import get_current_scans, get_queue_size

    return await get_queue_size() == 0 and not await get_current_scans()


async def _serve_proxy(server: uvicorn.Server) -> None:
    """Run the stream proxy without letting its failure take the service down.

    A busy port makes uvicorn call sys.exit; a proxy that cannot start must cost only Plex Web
    filtering, not the web UI, the scanner and the live skipping.
    """
    try:
        await server.serve()
    except (SystemExit, Exception) as exc:
        logger.error("Stream proxy stopped (%r); Plex Web filtering is off until the service restarts", exc)
        await asyncio.Event().wait()  # park: returning would read as "a loop died" and stop the service


async def _amain() -> None:
    global _exit_code
    # ── Bootstrap ──────────────────────────────────────────────────────────
    nice = ort_runtime.lower_process_priority()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db.set_db_path(DATA_DIR / "cleanplex.db")
    await db.init_db()

    config = await Config.load()
    setup_logging(config.log_level)

    # Mark any jobs left in running/queued state from a previous crashed process.
    await recover_stale_jobs()
    interrupted = await db.reset_interrupted_scans()
    if interrupted:
        logger.info("Reset %d scan job(s) interrupted by the last shutdown to pending", interrupted)

    pruned = await db.prune_skip_events(int(await db.get_setting("skip_event_retention_days", "90")))
    if pruned:
        logger.info("Pruned %d skip event(s) past the retention window", pruned)

    logger.info("Cleanplex starting — data dir: %s%s", DATA_DIR, f" (niceness {nice})" if nice else "")

    if config.is_configured():
        client = plex_mod.init_client(config.plex_url, config.plex_token)
        # Restore the transport each client last accepted, so the first skip after a
        # restart doesn't pay the full probe cost again.
        await client.load_client_profiles()
        logger.info("Plex client initialised: %s", config.plex_url)
    else:
        logger.warning("Plex not yet configured — open the web UI to set up.")

    # ── Shared config factory ──────────────────────────────────────────────
    async def get_config():
        return await Config.load()

    def get_client():
        return plex_mod.get_client()

    # ── Web server ─────────────────────────────────────────────────────────
    web_app = create_app()
    host = os.environ.get("CLEANPLEX_HOST", "0.0.0.0")
    port = int(os.environ.get("CLEANPLEX_PORT", "7979"))

    config_uvicorn = uvicorn.Config(
        web_app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
    )
    server = uvicorn.Server(config_uvicorn)

    logger.info("Web UI available at http://%s:%d", host, port)

    # Stream proxy (#132): the way to filter clients live skipping cannot reach, chiefly Plex
    # Web in a browser. Viewers open Plex through this port; 0 turns it off.
    proxy_port = int(os.environ.get("CLEANPLEX_PROXY_PORT", "7981"))
    proxy_server = None
    if proxy_port and config.is_configured():
        proxy_app = stream_proxy.create_proxy_app(
            config.plex_url, stream_proxy.db_policy(), keyframes_provider=stream_proxy.db_keyframes,
            session_store=stream_proxy.SessionStore(), part_lookup=stream_proxy.db_part_lookup,
            # nginx (the front door) sets X-Forwarded-For only for remote viewers and replaces any
            # client-sent value, so the header is trustworthy here.
            trust_forwarded_for=True,
        )
        # Behind the front door the proxy only needs localhost (CLEANPLEX_PROXY_HOST=127.0.0.1).
        proxy_host = os.environ.get("CLEANPLEX_PROXY_HOST", host)
        proxy_server = uvicorn.Server(uvicorn.Config(
            proxy_app, host=proxy_host, port=proxy_port, log_level="warning", access_log=False,
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
        ))
        logger.info("Plex Web with filtering: http://%s:%d/web", host, proxy_port)

    # ── Run all loops concurrently ─────────────────────────────────────────
    # The web server owns the signal handlers: SIGTERM/SIGINT make server.serve() return.
    # The other loops never return on their own, so a plain gather() kept the process alive
    # after the server stopped; cancel them explicitly once it does.
    server_task = asyncio.create_task(server.serve(), name="web-server")
    loop_tasks = [
        asyncio.create_task(coro, name=name)
        for name, coro in (
            *((("stream-proxy", _serve_proxy(proxy_server)),) if proxy_server else ()),
            ("session-watcher", session_watcher_loop(get_config, get_client)),
            ("library-watcher", library_watcher_loop(get_config, get_client)),
            ("scanner", scanner_loop(get_config)),
            ("hover-previews", hover_preview_loop(get_config, scanner_idle=_scanner_idle)),
            ("segment-cleanup", segment_cleanup_loop()),
            ("db-backup", db_backup_loop()),
            ("thumbnail-backfill", thumbnail_backfill_loop(get_config)),
            ("cast-sync", cast_sync_loop(get_config, get_client)),
            ("keyframe-index", keyframe_index_loop()),
            ("frontdoor-cert", frontdoor_cert_loop(DATA_DIR)),
        )
    ]
    # Stop when the server stops, or when any loop dies (which used to leave a half-working app).
    await asyncio.wait([server_task, *loop_tasks], return_when=asyncio.FIRST_COMPLETED)
    if not server_task.done():
        _exit_code = 1
    logger.info("Shutting down")
    _arm_hard_exit()
    server.should_exit = True
    if proxy_server:
        proxy_server.should_exit = True
    for task in loop_tasks:
        task.cancel()
    _, pending = await asyncio.wait([server_task, *loop_tasks], timeout=LOOP_CANCEL_TIMEOUT_S)
    for task in pending:
        logger.warning("Task %s did not stop in time", task.get_name())
    for task in [server_task, *loop_tasks]:
        if task.done() and not task.cancelled() and task.exception():
            logger.error("Task %s failed: %r", task.get_name(), task.exception())


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
    # Skip interpreter teardown, which joins non-daemon worker threads (a scan stuck inside
    # ffmpeg or onnxruntime would hold it open). Everything durable is in SQLite already.
    logging.shutdown()
    os._exit(_exit_code)
