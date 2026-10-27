"""FastAPI application factory."""

from __future__ import annotations

import traceback
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..logger import get_logger
from .routes.settings import router as settings_router
from .routes.sessions import router as sessions_router
from .routes.users import router as users_router
from .routes.segments import router as segments_router
from .routes.scanner_routes import router as scanner_router
from .routes.thumbnails import router as thumbnails_router
from .routes.sync_routes import router as sync_router
from .routes.mcp_routes import router as mcp_router
from .routes.analytics_routes import router as analytics_router
from .routes.marker_routes import router as marker_router
from .routes.import_routes import router as import_router
from .routes.events_routes import router as events_router

STATIC_DIR = Path(__file__).parent / "static"

# index.html names the current bundle, so the browser must revalidate it on every
# load (a cheap 304 via ETag); without this Safari kept serving the pre-deploy
# shell and its old JavaScript (#94).
_SHELL_CACHE = "no-cache"
# Vite puts a content hash in every asset file name, so a cached copy is never stale.
_ASSET_CACHE = "public, max-age=31536000, immutable"


class _HashedAssets(StaticFiles):
    """Static files whose names change whenever their content does."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            response.headers["Cache-Control"] = _ASSET_CACHE
        return response
logger = get_logger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Cleanplex",
        description="Plex content filter service",
        version="0.1.0",
    )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "Unhandled error %s %s — %s\n%s",
            request.method,
            request.url.path,
            exc,
            traceback.format_exc(),
        )
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(settings_router)
    app.include_router(sessions_router)
    app.include_router(users_router)
    app.include_router(segments_router)
    app.include_router(scanner_router)
    app.include_router(thumbnails_router)
    app.include_router(sync_router)
    app.include_router(mcp_router)
    app.include_router(analytics_router)
    app.include_router(marker_router)
    app.include_router(import_router)
    app.include_router(events_router)

    # Serve built React frontend (if present)
    if STATIC_DIR.exists():
        app.mount("/assets", _HashedAssets(directory=str(STATIC_DIR / "assets")), name="assets")

        @app.get("/", include_in_schema=False)
        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa(full_path: str = ""):
            index = STATIC_DIR / "index.html"
            if index.exists():
                return FileResponse(str(index), headers={"Cache-Control": _SHELL_CACHE})
            return {"message": "Cleanplex API running. Frontend not built yet."}

    return app
