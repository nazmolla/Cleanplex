"""Plex Media Server API wrapper."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

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
    thumb: str            # relative Plex thumb URL
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
    content_rating: str = ""  # e.g. "PG-13", "R", "TV-MA"


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
                        thumb=s.thumb or "",
                        library_section_id=section_id,
                    )
                )
            except Exception as exc:
                logger.debug("Error parsing session: %s", exc)

        return result

    # ── Seek ──────────────────────────────────────────────────────────────────

    async def seek(self, client_identifier: str, offset_ms: int) -> bool:
        """Send a seekTo command to a Plex client."""
        url = (
            f"{self.url}/player/playback/seekTo"
            f"?offset={offset_ms}"
            f"&clientIdentifier={client_identifier}"
            f"&commandID={int(time.time())}"
            f"&X-Plex-Token={self.token}"
        )
        try:
            resp = await self._http.get(url)
            if resp.status_code < 300:
                logger.info("Seeked client %s to %dms", client_identifier, offset_ms)
                return True
            logger.warning("Seek returned HTTP %d", resp.status_code)
            return False
        except Exception as exc:
            logger.warning("Seek failed: %s", exc)
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
            if hasattr(item, "grandparentTitle") and item.grandparentTitle:
                title = f"{item.grandparentTitle} – {item.parentTitle} – {item.title}"

            year = getattr(item, "year", None)

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

    async def close(self) -> None:
        await self._http.aclose()


# Module-level singleton (set by main.py after config loads)
_client: PlexClient | None = None


def get_client() -> PlexClient:
    if _client is None:
        raise RuntimeError("PlexClient not initialised. Call init_client() first.")
    return _client


def init_client(url: str, token: str) -> PlexClient:
    global _client
    _client = PlexClient(url, token)
    return _client
