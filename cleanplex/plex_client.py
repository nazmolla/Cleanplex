"""Plex Media Server API wrapper."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# TTL for per-show metadata cache; 10 minutes is long enough to cover a full
# library-titles request without stale data causing visible issues.
_SHOW_ART_CACHE_TTL_S = 600

import httpx
from plexapi.server import PlexServer
from plexapi.exceptions import PlexApiException

from .logger import get_logger

logger = get_logger(__name__)


@dataclass
class ActiveSession:
    session_key: str
    user: str
    title: str
    full_title: str
    plex_guid: str
    rating_key: str
    media_type: str       # "movie" or "episode"
    position_ms: int
    duration_ms: int
    client_identifier: str
    client_title: str
    is_controllable: bool
    thumb: str = ""       # relative Plex thumb URL
    client_address: str = ""
    client_port: int = 32500
    library_section_id: str = ""


@dataclass
class LibrarySection:
    section_id: str
    title: str
    section_type: str     # "movie" or "show"


@dataclass
class MediaItem:
    rating_key: str
    plex_guid: str
    title: str
    year: int | None
    thumb: str
    file_path: str
    library_id: str
    library_title: str
    media_type: str       # "movie" or "episode"
    content_rating: str = ""   # e.g. "PG-13", "R", "TV-MA"
    show_guid: str = ""        # grandparentGuid for episodes; empty for movies
    show_rating_key: str = ""  # grandparentRatingKey for episodes; used to build poster URLs from DB


@dataclass
class PlexUser:
    username: str
    thumb: str = ""
    is_home_user: bool = True


class PlexClient:
    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self._server: PlexServer | None = None
        self._http = httpx.AsyncClient(timeout=10)
        # {rating_key: (monotonic_timestamp, (show_guid, show_title, show_thumb, show_rating_key, season_rating_key))}
        self._show_art_cache: dict[str, tuple[float, tuple[str, str, str, str, str]]] = {}

    def _get_server(self) -> PlexServer:
        if self._server is None:
            self._server = PlexServer(self.url, self.token)
        return self._server

    def invalidate(self) -> None:
        self._server = None

    # ── Connectivity ──────────────────────────────────────────────────────────

    async def test_connection(self) -> tuple[bool, str]:
        try:
            srv = await asyncio.to_thread(self._get_server)
            return True, srv.friendlyName
        except Exception as exc:
            return False, str(exc)

    async def get_machine_identifier(self) -> str:
        """Return the Plex server machine identifier, used to build web deep links."""
        try:
            srv = await asyncio.to_thread(self._get_server)
            return str(srv.machineIdentifier)
        except Exception as exc:
            logger.debug("Failed to get machine identifier: %s", exc)
            return ""

    # ── Sessions ──────────────────────────────────────────────────────────────

    async def get_active_sessions(self) -> list[ActiveSession]:
        try:
            srv = await asyncio.to_thread(self._get_server)
            sessions = await asyncio.to_thread(srv.sessions)
        except Exception as exc:
            logger.warning("Failed to fetch sessions: %s", exc)
            return []

        result = []
        for s in sessions:
            try:
                # Determine full title
                if hasattr(s, "grandparentTitle") and s.grandparentTitle:
                    full_title = f"{s.grandparentTitle} – {s.parentTitle} – {s.title}"
                else:
                    full_title = s.title

                # Find the first player
                player = s.players[0] if s.players else None
                client_id = player.machineIdentifier if player else ""
                client_title = player.title if player else "Unknown"
                controllable = bool(player and player.state is not None) if player else False
                client_address = getattr(player, "address", "") if player else ""
                client_port_raw = getattr(player, "port", 32500) if player else 32500
                try:
                    client_port = int(client_port_raw or 32500)
                except Exception:
                    client_port = 32500

                # Resolve file path
                file_path = ""
                if s.media and s.media[0].parts:
                    file_path = s.media[0].parts[0].file or ""

                # GUID — prefer the first one that looks useful
                guid = ""
                if hasattr(s, "guids") and s.guids:
                    guid = s.guids[0].id
                elif hasattr(s, "guid"):
                    guid = s.guid or ""

                section_id = str(s.librarySectionID) if hasattr(s, "librarySectionID") else ""

                result.append(
                    ActiveSession(
                        session_key=str(s.sessionKey),
                        user=s.usernames[0] if s.usernames else "Unknown",
                        title=s.title,
                        full_title=full_title,
                        plex_guid=guid,
                        rating_key=str(s.ratingKey),
                        media_type=s.type,
                        position_ms=int(s.viewOffset or 0),
                        duration_ms=int(s.duration or 0),
                        client_identifier=client_id,
                        client_title=client_title,
                        is_controllable=controllable,
                        client_address=client_address,
                        client_port=client_port,
                        thumb=s.thumb or "",
                        library_section_id=section_id,
                    )
                )
            except Exception as exc:
                logger.debug("Error parsing session: %s", exc)

        return result

    # ── Seek ──────────────────────────────────────────────────────────────────

    async def seek(self, client_identifier: str, offset_ms: int, client_address: str = "", client_port: int = 32500) -> bool:
        """Seek via server proxy first, then try direct client control as fallback."""
        try:
            srv = await asyncio.to_thread(self._get_server)
            key = (
                f"/player/playback/seekTo"
                f"?offset={offset_ms}"
                f"&type=video"
                f"&commandID={int(time.time())}"
            )
            headers = {"X-Plex-Target-Client-Identifier": client_identifier}
            await asyncio.to_thread(srv.query, key, headers=headers)
            logger.info("Seeked client %s to %dms via server query proxy", client_identifier, offset_ms)
            return True
        except Exception as exc:
            logger.warning("Proxy seek failed for %s: %s", client_identifier, exc)

        if not client_address:
            logger.warning("No client_address available for direct seek fallback (client=%s)", client_identifier)
            return False

        ports = [client_port, 32500, 3005]
        seen: set[int] = set()
        for port in ports:
            if port in seen:
                continue
            seen.add(port)
            base = (
                f"http://{client_address}:{port}/player/playback/seekTo"
                f"?offset={offset_ms}"
                f"&type=video"
                f"&commandID={int(time.time())}"
            )
            variants = [
                (
                    f"{base}&X-Plex-Token={self.token}",
                    {"X-Plex-Target-Client-Identifier": client_identifier},
                ),
                (
                    base,
                    {
                        "X-Plex-Token": self.token,
                        "X-Plex-Target-Client-Identifier": client_identifier,
                    },
                ),
                (
                    f"{base}&X-Plex-Token={self.token}",
                    {
                        "X-Plex-Target-Client-Identifier": client_identifier,
                        "X-Plex-Client-Identifier": "cleanplex-server",
                        "X-Plex-Product": "Cleanplex",
                        "X-Plex-Device-Name": "Cleanplex",
                        "X-Plex-Platform": "Windows",
                    },
                ),
                (
                    base,
                    {
                        "X-Plex-Token": self.token,
                        "X-Plex-Target-Client-Identifier": client_identifier,
                        "X-Plex-Client-Identifier": "cleanplex-server",
                        "X-Plex-Product": "Cleanplex",
                        "X-Plex-Device-Name": "Cleanplex",
                        "X-Plex-Platform": "Windows",
                    },
                ),
            ]

            for idx, (url, headers) in enumerate(variants, start=1):
                try:
                    resp = await self._http.get(url, headers=headers)
                    if resp.status_code < 300:
                        logger.info(
                            "Seeked client %s directly at %s:%d to %dms (variant=%d)",
                            client_identifier,
                            client_address,
                            port,
                            offset_ms,
                            idx,
                        )
                        return True
                    logger.warning(
                        "Direct seek HTTP %d for client %s at %s:%d (variant=%d, body=%s)",
                        resp.status_code,
                        client_identifier,
                        client_address,
                        port,
                        idx,
                        resp.text[:500],
                    )
                except Exception as exc:
                    logger.warning(
                        "Direct seek failed for client %s at %s:%d (variant=%d): %s",
                        client_identifier,
                        client_address,
                        port,
                        idx,
                        exc,
                    )

        return False

    # ── Library ───────────────────────────────────────────────────────────────

    async def get_library_sections(self) -> list[LibrarySection]:
        try:
            srv = await asyncio.to_thread(self._get_server)
            sections = await asyncio.to_thread(srv.library.sections)
            return [
                LibrarySection(
                    section_id=str(s.key),
                    title=s.title,
                    section_type=s.type,
                )
                for s in sections
                if s.type in ("movie", "show")
            ]
        except Exception as exc:
            logger.warning("Failed to fetch library sections: %s", exc)
            return []

    async def get_library_items(self, section_id: str) -> list[MediaItem]:
        try:
            srv = await asyncio.to_thread(self._get_server)
            section = await asyncio.to_thread(srv.library.sectionByID, int(section_id))
            all_items = await asyncio.to_thread(section.all)
        except Exception as exc:
            logger.warning("Failed to fetch library items for section %s: %s", section_id, exc)
            return []

        result = []
        for item in all_items:
            try:
                if item.type == "show":
                    # Enumerate all episodes
                    episodes = await asyncio.to_thread(item.episodes)
                    for ep in episodes:
                        media_item = self._media_item_from_plex(ep, section_id, section.title)
                        if media_item:
                            result.append(media_item)
                else:
                    media_item = self._media_item_from_plex(item, section_id, section.title)
                    if media_item:
                        result.append(media_item)
            except Exception as exc:
                logger.debug("Error parsing library item: %s", exc)

        return result

    def _media_item_from_plex(self, item: Any, library_id: str, library_title: str) -> MediaItem | None:
        try:
            file_path = ""
            if item.media and item.media[0].parts:
                file_path = item.media[0].parts[0].file or ""

            guid = ""
            if hasattr(item, "guids") and item.guids:
                guid = item.guids[0].id
            elif hasattr(item, "guid"):
                guid = item.guid or ""

            title = item.title
            # For episodes, include full show/season/episode context
            if hasattr(item, "grandparentTitle") and item.grandparentTitle:
                title = f"{item.grandparentTitle} – {item.parentTitle} – {item.title}"

            year = getattr(item, "year", None)

            show_guid = getattr(item, "grandparentGuid", "") or ""
            show_rating_key = str(getattr(item, "grandparentRatingKey", "") or "")

            return MediaItem(
                rating_key=str(item.ratingKey),
                plex_guid=guid,
                title=title,
                year=year,
                thumb=item.thumb or "",
                file_path=file_path,
                library_id=library_id,
                library_title=library_title,
                media_type=item.type,
                content_rating=getattr(item, "contentRating", "") or "",
                show_guid=show_guid,
                show_rating_key=show_rating_key,
            )
        except Exception:
            return None

    # ── Users ─────────────────────────────────────────────────────────────────

    async def get_all_users(self) -> list[PlexUser]:
        try:
            srv = await asyncio.to_thread(self._get_server)
            # Home users / managed users
            users: list[PlexUser] = []
            try:
                home_users = await asyncio.to_thread(srv.myPlexAccount().users)
                for u in home_users:
                    users.append(PlexUser(username=u.username or u.title, thumb=u.thumb or ""))
            except Exception:
                pass
            # Also add the owner
            try:
                account = await asyncio.to_thread(srv.myPlexAccount)
                users.insert(0, PlexUser(username=account.username, thumb=account.thumb or "", is_home_user=False))
            except Exception:
                pass
            return users
        except Exception as exc:
            logger.warning("Failed to fetch users: %s", exc)
            return []

    # ── Thumbnail proxy URL ───────────────────────────────────────────────────

    def thumb_url(self, thumb_path: str) -> str:
        if not thumb_path:
            return ""
        return f"{self.url}{thumb_path}?X-Plex-Token={self.token}"

    async def get_episode_show_art(self, rating_key: str) -> tuple[str, str, str, str, str]:
        """Return (show_guid, show_title, show_thumb_path, show_rating_key, season_rating_key) for an episode rating key.

        Results are cached per rating_key with a TTL of _SHOW_ART_CACHE_TTL_S seconds
        to avoid redundant Plex API calls when listing large TV libraries.
        """
        now = time.monotonic()
        cached = self._show_art_cache.get(rating_key)
        if cached and now - cached[0] < _SHOW_ART_CACHE_TTL_S:
            return cached[1]

        try:
            srv = await asyncio.to_thread(self._get_server)
            item = await asyncio.to_thread(srv.fetchItem, int(rating_key))
            show_guid = getattr(item, "grandparentGuid", "") or ""
            show_title = getattr(item, "grandparentTitle", "") or ""
            show_thumb = getattr(item, "grandparentThumb", "") or ""
            show_rating_key = str(getattr(item, "grandparentRatingKey", "") or "")
            season_rating_key = str(getattr(item, "parentRatingKey", "") or "")
            result = (show_guid, show_title, show_thumb, show_rating_key, season_rating_key)
            self._show_art_cache[rating_key] = (now, result)
            return result
        except Exception as exc:
            logger.debug("Failed to resolve show art for rating_key %s: %s", rating_key, exc)
            return "", "", "", "", ""

    async def fetch_image(self, image_path: str) -> tuple[bytes, str]:
        """Fetch an image from Plex and return (bytes, content_type)."""
        if not image_path:
            return b"", ""

        path = image_path if image_path.startswith("/") else f"/{image_path}"
        url = f"{self.url}{path}"
        if "X-Plex-Token=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}X-Plex-Token={self.token}"

        resp = await self._http.get(url)
        if resp.status_code >= 400:
            return b"", ""

        return resp.content, resp.headers.get("content-type", "image/jpeg")

    # ── Cleanplex metadata block ──────────────────────────────────────────────

    def _strip_cleanplex_block(self, summary: str) -> str:
        pattern = r"\n*\[\[CLEANPLEX\]\].*?\[\[/CLEANPLEX\]\]\n*"
        return re.sub(pattern, "\n", summary or "", flags=re.S).strip()

    def _build_cleanplex_block(self, status: str, segment_count: int, last_scan: str | None = None) -> str:
        stamp = last_scan or datetime.now().strftime("%Y-%m-%d %H:%M")
        return (
            "[[CLEANPLEX]]\n"
            "Cleanplex Scan\n"
            f"Status: {status}\n"
            f"Segments: {segment_count}\n"
            f"Last Scan: {stamp}\n"
            "[[/CLEANPLEX]]"
        )

    async def update_cleanplex_summary(
        self,
        rating_key: str,
        status: str,
        segment_count: int,
        last_scan: str | None = None,
    ) -> bool:
        """Insert/update a marker-based Cleanplex block in Plex summary metadata."""
        try:
            srv = await asyncio.to_thread(self._get_server)
            item = await asyncio.to_thread(srv.fetchItem, int(rating_key))
            current_summary = getattr(item, "summary", "") or ""

            base_summary = self._strip_cleanplex_block(current_summary)
            cleanplex_block = self._build_cleanplex_block(status, segment_count, last_scan)
            new_summary = f"{base_summary}\n\n{cleanplex_block}".strip() if base_summary else cleanplex_block

            try:
                await asyncio.to_thread(item.editSummary, new_summary)
            except Exception:
                await asyncio.to_thread(item.edit, summary=new_summary)

            logger.info("Updated Plex summary metadata for rating_key=%s", rating_key)
            return True
        except Exception as exc:
            logger.warning("Failed to update Plex summary metadata for rating_key=%s: %s", rating_key, exc)
            return False

    async def close(self) -> None:
        await self._http.aclose()


# Module-level singleton (set by main.py after config loads)
_client: PlexClient | None = None


def get_client() -> PlexClient:
    if _client is None:
        raise RuntimeError("PlexClient not initialised. Call init_client() first.")
    return _client


def init_client(url: str, token: str) -> PlexClient:
    """Create (or replace) the module-level PlexClient singleton.

    The previous AsyncClient is closed before replacement so open connections
    are not leaked on settings changes or reconnects.
    """
    global _client
    if _client is not None:
        # Schedule close on the running event loop without blocking the caller.
        try:
            loop = asyncio.get_event_loop()
            if not loop.is_closed():
                loop.create_task(_client.close())
        except RuntimeError:
            pass  # no running loop — process is tearing down
    _client = PlexClient(url, token)
    return _client
