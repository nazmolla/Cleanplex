"""Cleanplex entry point — starts the watcher loops and web server."""

from __future__ import annotations

import asyncio
import os
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
from .thumbnail_backfill import thumbnail_backfill_loop
from .web.app import create_app
from .bg_jobs import recover_stale_jobs

DATA_DIR = Path(os.environ.get("CLEANPLEX_DATA", Path.home() / ".cleanplex"))
logger = get_logger(__name__)


def _scanner_idle() -> bool:
    """True when no scan is queued or running, which is when hover previews may render."""
    from .scanner import get_current_scans, get_queue_size

    return get_queue_size() == 0 and not get_current_scans()


async def _amain() -> None:
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
    )
    server = uvicorn.Server(config_uvicorn)

    logger.info("Web UI available at http://%s:%d", host, port)

    # ── Run all loops concurrently ─────────────────────────────────────────
    await asyncio.gather(
        server.serve(),
        session_watcher_loop(get_config, get_client),
        library_watcher_loop(get_config, get_client),
        scanner_loop(get_config),
        hover_preview_loop(get_config, scanner_idle=_scanner_idle),
        segment_cleanup_loop(),
        thumbnail_backfill_loop(get_config),
    )


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
