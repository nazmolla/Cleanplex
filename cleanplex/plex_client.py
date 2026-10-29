"""Plex Media Server API wrapper."""

from __future__ import annotations

import asyncio
import json
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
    # Plex reports the player as on the server's own network. A remote player can show
    # a state yet cannot be reached by the server's commands.
    client_local: bool = True
    library_section_id: str = ""
    # Player volume 0-100, or None when the client does not report one. Captured so
    # a mute can restore the viewer's own level rather than guessing 100.
    volume: int | None = None
    # ISO code of the audio track being played, empty when it cannot be determined.
    # Language-specific segments (profanity mutes) only apply to their own track.
    audio_language: str = ""


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
    duration_ms: int = 0       # runtime, used to sanity-check imported skip files
    # Ordered list of all part file paths for multi-part movies (e.g. CD1/CD2).
    # file_path holds parts[0]; part_files holds the full list.
    # Empty for single-file titles.
    part_files: list = field(default_factory=list)


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
        # {client_identifier: {"transport": "proxy"|"direct", "port": int, "variant": int}}
        # How each client last accepted a player command. Populated by
        # load_client_profiles() at startup and updated whenever one is learned.
        self._client_profiles: dict[str, dict] = {}

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
                client_local = bool(getattr(player, "local", True)) if player else False
                client_port_raw = getattr(player, "port", 32500) if player else 32500
                try:
                    client_port = int(client_port_raw or 32500)
                except Exception:
                    client_port = 32500

                volume_raw = getattr(player, "volume", None) if player else None
                try:
                    volume = int(volume_raw) if volume_raw is not None else None
                except Exception:
                    volume = None

                # Resolve file path
                file_path = ""
                audio_language = ""
                if s.media and s.media[0].parts:
                    part = s.media[0].parts[0]
                    file_path = part.file or ""
                    # The selected stream is what the viewer actually hears; fall
                    # back to the first audio stream when Plex marks none selected.
                    streams = [st for st in (getattr(part, "audioStreams", None) or [])]
                    selected = next((st for st in streams if getattr(st, "selected", False)), None)
                    chosen = selected or (streams[0] if streams else None)
                    if chosen is not None:
                        audio_language = (
                            getattr(chosen, "languageCode", "") or getattr(chosen, "language", "") or ""
                        ).lower()

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
                        client_local=client_local,
                        thumb=s.thumb or "",
                        library_section_id=section_id,
                        volume=volume,
                        audio_language=audio_language,
                    )
                )
            except Exception as exc:
                logger.debug("Error parsing session: %s", exc)

        return result

    # ── Player commands ───────────────────────────────────────────────────────

    async def load_client_profiles(self) -> None:
        """Restore learned per-client transports so a restart does not re-probe."""
        from . import database as db

        try:
            raw = await db.get_setting("client_seek_profiles", "{}")
            loaded = json.loads(raw or "{}")
            if isinstance(loaded, dict):
                self._client_profiles = {k: v for k, v in loaded.items() if isinstance(v, dict)}
        except Exception as exc:
            logger.debug("Could not load client seek profiles: %s", exc)

    async def _persist_profiles(self) -> None:
        """Write profiles back to settings.

        Awaited rather than fired off as a task: a detached write can outlive the
        loop it was created on. Profiles change rarely — only when a client's
        working transport changes — so the cost sits outside the hot path.
        """
        from . import database as db

        try:
            await db.set_setting("client_seek_profiles", json.dumps(self._client_profiles))
        except Exception as exc:
            # Persistence is best-effort; the in-memory cache still serves this run.
            logger.debug("Could not persist client seek profiles: %s", exc)

    async def _remember_profile(self, client_identifier: str, profile: dict) -> None:
        if self._client_profiles.get(client_identifier) == profile:
            return
        self._client_profiles[client_identifier] = profile
        await self._persist_profiles()

    async def _forget_profile(self, client_identifier: str) -> None:
        if self._client_profiles.pop(client_identifier, None) is not None:
            await self._persist_profiles()

    # Direct-control fallbacks, ordered cheapest-first. A client that needs one of
    # the later variants needs it every time, so the winning combination is cached
    # per client rather than rediscovered on each command — see _send_command.
    _FALLBACK_PORTS = (32500, 3005)

    def _direct_variants(self, base: str, client_identifier: str) -> list[tuple[str, dict]]:
        """Return (url, headers) pairs to try when the server proxy will not relay."""
        full_headers = {
            "X-Plex-Target-Client-Identifier": client_identifier,
            "X-Plex-Client-Identifier": "cleanplex-server",
            "X-Plex-Product": "Cleanplex",
            "X-Plex-Device-Name": "Cleanplex",
            "X-Plex-Platform": "Windows",
        }
        return [
            (f"{base}&X-Plex-Token={self.token}", {"X-Plex-Target-Client-Identifier": client_identifier}),
            (base, {"X-Plex-Token": self.token, "X-Plex-Target-Client-Identifier": client_identifier}),
            (f"{base}&X-Plex-Token={self.token}", full_headers),
            (base, {"X-Plex-Token": self.token, **full_headers}),
        ]

    async def _try_proxy(self, path: str, client_identifier: str) -> bool:
        """Relay a player command through the server. Works for most modern clients."""
        try:
            srv = await asyncio.to_thread(self._get_server)
            headers = {"X-Plex-Target-Client-Identifier": client_identifier}
            await asyncio.to_thread(srv.query, path, headers=headers)
            return True
        except Exception as exc:
            logger.warning("Proxy %s failed for %s: %s", path.split("?")[0], client_identifier, exc)
            return False

    async def _try_direct(
        self, path: str, client_identifier: str, address: str, port: int, variant: int
    ) -> bool:
        """Send one specific direct-control attempt. Returns True on a 2xx response."""
        base = f"http://{address}:{port}{path}"
        variants = self._direct_variants(base, client_identifier)
        if variant >= len(variants):
            return False
        url, headers = variants[variant]
        try:
            resp = await self._http.get(url, headers=headers)
            if resp.status_code < 300:
                return True
            logger.warning(
                "Direct command HTTP %d for client %s at %s:%d (variant=%d, body=%s)",
                resp.status_code, client_identifier, address, port, variant + 1, resp.text[:500],
            )
        except Exception as exc:
            logger.warning(
                "Direct command failed for client %s at %s:%d (variant=%d): %s",
                client_identifier, address, port, variant + 1, exc,
            )
        return False

    async def _send_command(
        self, path: str, client_identifier: str, client_address: str = "", client_port: int = 32500
    ) -> bool:
        """Send a player command, preferring the transport that last worked for this client.

        Without the cache every command on a proxy-averse client re-runs the full
        search — up to 12 requests — before landing, which is exactly when latency
        matters most.
        """
        profile = self._client_profiles.get(client_identifier)

        if profile is not None:
            if profile.get("transport") == "proxy":
                if await self._try_proxy(path, client_identifier):
                    return True
            elif client_address:
                if await self._try_direct(
                    path, client_identifier, client_address,
                    int(profile.get("port", client_port)), int(profile.get("variant", 0)),
                ):
                    return True
            # The learned path stopped working (client restarted, port changed):
            # fall through to a full search and re-learn.
            logger.info("Cached transport failed for %s — re-probing", client_identifier)
            await self._forget_profile(client_identifier)

        if await self._try_proxy(path, client_identifier):
            await self._remember_profile(client_identifier, {"transport": "proxy"})
            return True

        if not client_address:
            logger.warning("No client_address available for direct fallback (client=%s)", client_identifier)
            return False

        ports: list[int] = []
        for port in (client_port, *self._FALLBACK_PORTS):
            if port and port not in ports:
                ports.append(port)

        for port in ports:
            for variant in range(len(self._direct_variants("http://x", client_identifier))):
                if await self._try_direct(path, client_identifier, client_address, port, variant):
                    logger.info(
                        "Client %s reachable directly at %s:%d (variant=%d)",
                        client_identifier, client_address, port, variant + 1,
                    )
                    await self._remember_profile(
                        client_identifier,
                        {"transport": "direct", "port": port, "variant": variant},
                    )
                    return True

        return False

    async def seek(self, client_identifier: str, offset_ms: int, client_address: str = "", client_port: int = 32500) -> bool:
        """Seek the given client to offset_ms. Returns True if a command was accepted."""
        path = (
            f"/player/playback/seekTo?offset={offset_ms}&type=video"
            f"&commandID={int(time.time())}"
        )
        success = await self._send_command(path, client_identifier, client_address, client_port)
        if success:
            logger.info("Seeked client %s to %dms", client_identifier, offset_ms)
        return success

    async def set_volume(self, client_identifier: str, level: int, client_address: str = "", client_port: int = 32500) -> bool:
        """Set the client's playback volume (0-100). Used to mute rather than skip."""
        level = max(0, min(100, int(level)))
        path = (
            f"/player/playback/setParameters?volume={level}&type=video"
            f"&commandID={int(time.time())}"
        )
        success = await self._send_command(path, client_identifier, client_address, client_port)
        if success:
            logger.info("Set volume on client %s to %d", client_identifier, level)
        return success

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
        except Exception as exc:
            logger.warning("Failed to fetch library section %s: %s", section_id, exc)
            return []

        try:
            if section.type == "show":
                # One bulk call for all episodes — avoids N per-show API calls.
                raw_items = await asyncio.to_thread(lambda: section.search(libtype="episode"))
            else:
                raw_items = await asyncio.to_thread(section.all)
        except Exception as exc:
            logger.warning("Failed to fetch items for section %s: %s", section_id, exc)
            return []

        result = []
        for item in raw_items:
            try:
                media_item = self._media_item_from_plex(item, section_id, section.title)
                if media_item:
                    result.append(media_item)
            except Exception as exc:
                logger.debug("Error parsing library item: %s", exc)

        return result

    def _media_item_from_plex(self, item: Any, library_id: str, library_title: str) -> MediaItem | None:
        try:
            # For multi-version items, try each media[] entry until we find one
            # whose primary part file exists on disk. This prevents scan jobs
            # pointing to stale/missing files when a version was removed.
            file_path = ""
            chosen_media = None
            for media in (item.media or []):
                if media.parts and media.parts[0].file:
                    file_path = media.parts[0].file
                    chosen_media = media
                    break

            # Collect all part file paths from the chosen media version.
            # Multi-part movies (CD1/CD2) have multiple parts[] under one media[].
            part_files: list[str] = []
            if chosen_media and chosen_media.parts:
                part_files = [p.file for p in chosen_media.parts if p.file]

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
                part_files=part_files,
                duration_ms=int(getattr(item, "duration", 0) or 0),
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

    async def get_markers(self, rating_key: str) -> list[dict]:
        """Return intro/credits markers for a Plex item as a list of dicts.

        Each dict contains: plex_marker_id, marker_type, start_ms, end_ms, final.
        Returns empty list if the item has no markers or markers attribute is absent.
        """
        try:
            srv = await asyncio.to_thread(self._get_server)
            item = await asyncio.to_thread(srv.fetchItem, int(rating_key))
            raw_markers = await asyncio.to_thread(lambda: getattr(item, "markers", []))
            result = []
            for m in raw_markers:
                result.append({
                    "plex_marker_id": getattr(m, "id", None),
                    "marker_type": getattr(m, "type", "unknown"),
                    "start_ms": int(getattr(m, "start", 0)),
                    "end_ms": int(getattr(m, "end", 0)),
                    "final": bool(getattr(m, "final", False)),
                })
            return result
        except Exception as exc:
            logger.error("get_markers failed for rating_key=%s: %s", rating_key, exc)
            raise

    async def update_marker(self, rating_key: str, plex_marker_id: int, start_ms: int, end_ms: int) -> None:
        """Write updated marker timestamps back to the Plex server.

        Raises PermissionError if Plex rejects due to Plex Pass restriction.
        Raises RuntimeError for other Plex API failures.
        """
        try:
            srv = await asyncio.to_thread(self._get_server)
            item = await asyncio.to_thread(srv.fetchItem, int(rating_key))
            raw_markers = await asyncio.to_thread(lambda: getattr(item, "markers", []))
            target = next((m for m in raw_markers if getattr(m, "id", None) == plex_marker_id), None)
            if target is None:
                raise RuntimeError(f"Marker {plex_marker_id} not found on rating_key={rating_key}")
            await asyncio.to_thread(target.edit, startTimeOffset=start_ms, endTimeOffset=end_ms)
            logger.info("Updated Plex marker %s on rating_key=%s: %d–%d ms", plex_marker_id, rating_key, start_ms, end_ms)
        except Exception as exc:
            msg = str(exc).lower()
            if "unauthorized" in msg or "403" in msg or "plex pass" in msg or "subscription" in msg:
                raise PermissionError(f"Plex Pass required to edit markers: {exc}") from exc
            raise RuntimeError(f"Plex marker update failed: {exc}") from exc

    async def create_marker(self, rating_key: str, marker_type: str, start_ms: int, end_ms: int) -> None:
        """Create a new intro/credits marker on a Plex item via the undocumented markers API.

        Requires Plex Pass. Raises PermissionError if subscription check fails.
        """
        params = {
            "type": marker_type,
            "startTimeOffset": start_ms,
            "endTimeOffset": end_ms,
            "X-Plex-Token": self.token,
        }
        resp = await self._http.post(f"{self.url}/library/metadata/{rating_key}/markers", params=params)
        if resp.status_code in (401, 403):
            raise PermissionError(f"Plex Pass required to create markers (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise RuntimeError(f"Plex marker create failed: HTTP {resp.status_code} — {resp.text[:200]}")
        logger.info("Created Plex marker type=%s on rating_key=%s: %d–%d ms", marker_type, rating_key, start_ms, end_ms)

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
