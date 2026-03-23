"""Cleanplex entry point — starts the watcher loops and web server."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import uvicorn

from .logger import setup_logging, get_logger
from . import database as db
from .config import Config
import cleanplex.plex_client as plex_mod
from .watcher import session_watcher_loop, library_watcher_loop
from .scanner import scanner_loop
from .web.app import create_app

DATA_DIR = Path(os.environ.get("CLEANPLEX_DATA", Path.home() / ".cleanplex"))
logger = get_logger(__name__)


async def _amain() -> None:
    # ── Bootstrap ──────────────────────────────────────────────────────────
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db.set_db_path(DATA_DIR / "cleanplex.db")
    await db.init_db()

    config = await Config.load()
    setup_logging(config.log_level)

    logger.info("Cleanplex starting — data dir: %s", DATA_DIR)

    if config.is_configured():
        plex_mod.init_client(config.plex_url, config.plex_token)
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
    )


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
