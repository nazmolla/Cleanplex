"""Prototype reverse proxy that removes filtered ranges from Plex HLS streams (#132).

A Plex HLS transcode publishes its whole playlist up front, so a proxy in front of
Plex can drop the segments covering flagged ranges before the client ever asks for
them. That works for any client that plays Plex's HLS stream, on any network,
without a second copy of the file. Seeks and progress reports run in the player's
shortened timeline, so the module also maps times between the original and the edited
stream.

Playback is forced through a re-encoding HLS transcode (directPlay/directStream off):
in that mode Plex cuts segments on an exact grid (the PTS of segment i is the sum of
the preceding EXTINF durations), which a remux does not guarantee. See #132 for the
measurements.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import ipaddress
import os
import re
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from .logger import get_logger

logger = get_logger(__name__)

Range = tuple[int, int]  # (start_ms, end_ms) in the original timeline

_EXTINF = re.compile(r"#EXTINF:([\d.]+)")
_RATING_KEY = re.compile(r"/library/metadata/(\d+)")
_START_PATHS = {
    "/video/:/transcode/universal/start.m3u8", "/video/:/transcode/universal/start", "/video/:/transcode/universal/start.mpd",
}
# A direct file request (direct play, download) names a Plex part id. Preview thumbnails
# (/library/parts/<id>/indexes/...) are left alone: a scrub preview is a minor leak and the
# owner chose to keep previews (2026-11-05).
_PART = re.compile(r"^/library/parts/(\d+)/(?!indexes/)")
# Cached allow/deny of direct file requests per (token, rating key): players issue many range requests.
_PART_DECISION_TTL_S = 60
_DECISION_PATH = "/video/:/transcode/universal/decision"
# Soft subtitles for a transcoded session come as one file on the film's timeline (Plex Web's
# resolveSubtitlesUrl); they must be shifted like the video or they run ahead after a cut.
_SUBTITLES_PATH = "/video/:/transcode/universal/subtitles"
# Requests that carry the player position (milliseconds) for Plex to store as the resume point.
_PROGRESS_PATHS = {"/:/timeline", "/:/progress"}
_VARIANT = re.compile(r"^/video/:/transcode/universal/session/([^/]+)/base/index\.m3u8$")
_SEGMENT = re.compile(r"^/video/:/transcode/universal/session/([^/]+)/base/(\d+)\.ts$")
# Headers that describe one hop, not the message; they must not be copied through.
_HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "host"}


def parse_cuts(spec: str) -> list[Range]:
    """Parse "60000-75000,300000-310000" (milliseconds) into ranges; blanks are ignored."""
    ranges = []
    for part in filter(None, (p.strip() for p in spec.split(","))):
        start, _, end = part.partition("-")
        ranges.append((int(start), int(end)))
    return ranges


@dataclass
class PlaylistEdit:
    """Result of editing a variant playlist."""

    text: str
    removed: list[int] = field(default_factory=list)  # segment indexes dropped
    spans: list[Range] = field(default_factory=list)  # removed time, original timeline, merged
    kept: int = 0

    @property
    def removed_ms(self) -> int:
        return sum(end - start for start, end in self.spans)


# In copy mode Plex may start a segment on a keyframe up to this long before the playlist's
# nominal boundary (measured 1.2 s on a 10 s-segment stream, #184); bounds are widened by it.
COPY_BOUNDARY_TOLERANCE_MS = 1500


def _first_at_or_after(keyframes: list[int], t_ms: float) -> float:
    i = bisect.bisect_left(keyframes, t_ms)
    return keyframes[i] if i < len(keyframes) else float("inf")


def edit_playlist(
    text: str, ranges: list[Range], offset_ms: int = 0, keyframes: list[int] | None = None,
) -> PlaylistEdit:
    """Drop every segment that overlaps one of *ranges* from a Plex variant playlist.

    A segment's time is the running sum of EXTINF durations, starting at *offset_ms*
    (the `offset` the stream was started at). Whole segments are removed, so the cut
    always widens outward to segment boundaries and never leaves flagged frames in.
    #EXT-X-DISCONTINUITY is inserted where a gap was cut, since the next segment's
    timestamps jump.

    With *keyframes* (ms, file time) the stream is a copy, whose segments start on a source
    keyframe near the nominal time rather than exactly on it. A segment then counts as
    overlapping when anything from its earliest possible start (first keyframe at or after
    nominal start minus the tolerance) to its latest possible end (first keyframe at or
    after the nominal end) touches a range. The returned spans stay nominal, because the
    player's clock runs on the EXTINF durations it was given.
    """
    lines = text.splitlines()
    out: list[str] = []
    removed: list[int] = []
    spans: list[Range] = []
    kept = 0
    cursor_ms = float(offset_ms)
    index = 0
    after_gap = False
    i = 0
    while i < len(lines):
        match = _EXTINF.match(lines[i])
        if not match:
            out.append(lines[i])
            i += 1
            continue
        duration_ms = float(match.group(1)) * 1000
        uri = lines[i + 1] if i + 1 < len(lines) else ""
        start, end = cursor_ms, cursor_ms + duration_ms
        lo, hi = start, end
        if keyframes:
            lo = _first_at_or_after(keyframes, start - COPY_BOUNDARY_TOLERANCE_MS)
            hi = _first_at_or_after(keyframes, end)
        if any(lo < r_end and hi > r_start for r_start, r_end in ranges):
            removed.append(index)
            if spans and spans[-1][1] == round(start):
                spans[-1] = (spans[-1][0], round(end))
            else:
                spans.append((round(start), round(end)))
            after_gap = kept > 0
        else:
            if after_gap:
                out.append("#EXT-X-DISCONTINUITY")
                after_gap = False
            out.extend((lines[i], uri))
            kept += 1
        cursor_ms = end
        index += 1
        i += 2
    return PlaylistEdit("\n".join(out) + "\n", removed, spans, kept)


def original_to_edited(t_ms: int, spans: list[Range]) -> int:
    """Map an original-timeline position to the shortened stream's position.

    A position inside a removed span lands where the cut is.
    """
    shift = 0
    for start, end in spans:
        if t_ms >= end:
            shift += end - start
        elif t_ms > start:
            shift += t_ms - start
    return t_ms - shift


def edited_to_original(t_ms: int, spans: list[Range]) -> int:
    """Map a position in the shortened stream back to the original timeline."""
    shift = 0
    for start, end in spans:
        if t_ms + shift >= start:
            shift += end - start
        else:
            break
    return t_ms + shift


_SRT_VTT_CUE = re.compile(
    r"(?P<a>(?:\d+:)?\d{2}:\d{2}[,.]\d{3})\s*-->\s*(?P<b>(?:\d+:)?\d{2}:\d{2}[,.]\d{3})(?P<rest>.*)"
)


def _parse_clock(text: str) -> int:
    """'H:MM:SS.cc', 'HH:MM:SS,mmm' or 'MM:SS.mmm' -> ms."""
    head, frac = re.split(r"[.,]", text)
    parts = [int(p) for p in head.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts
    ms = int(frac) * (10 if len(frac) == 2 else 1)
    return ((h * 60 + m) * 60 + sec) * 1000 + ms


def _format_clock(ms: int, style: str) -> str:
    ms = max(0, ms)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    sec, frac = divmod(rem, 1000)
    if style == "ass":
        return f"{h}:{m:02d}:{sec:02d}.{frac // 10:02d}"
    sep = "," if style == "srt" else "."
    return f"{h:02d}:{m:02d}:{sec:02d}{sep}{frac:03d}"


class SubtitleShifter:
    """Re-time a subtitle stream (ASS, SRT or WebVTT) for a stream with *spans* removed, as it arrives.

    Plex sends a transcoded session's subtitles progressively, a chunk at a time while the
    transcode runs, so the response must be rewritten line by line rather than read whole.
    Cue times are taken as relative to the stream start (*offset_ms* into the film). A cue
    wholly inside a removed span is dropped; others move back by the time removed before
    them, exactly as original_to_edited moves the video.
    """

    def __init__(self, spans: list[Range], offset_ms: int = 0):
        self.spans = spans
        self.offset = offset_ms
        self.base = original_to_edited(offset_ms, spans) if spans else 0
        self._buf = ""
        self._style: str | None = None   # "ass", "srt" or "vtt", decided from the first content
        self._held: str | None = None    # an SRT cue number, kept until its timing line is seen
        self._dropping = False           # inside a dropped SRT/VTT cue: skip until a blank line

    def _remap(self, t_ms: int) -> int:
        return original_to_edited(t_ms + self.offset, self.spans) - self.base

    def _inside(self, a: int, b: int) -> bool:
        return any(a + self.offset >= s and b + self.offset <= e for s, e in self.spans)

    def _line(self, line: str) -> list[str]:
        if self._style is None and line.strip():
            stripped = line.strip()
            self._style = ("vtt" if stripped.startswith("WEBVTT")
                           else "ass" if stripped.startswith("[") or stripped.startswith("Dialogue:") else "srt")
        if self._style == "ass":
            if line.startswith("Dialogue:"):
                head, rest = line.split(":", 1)
                fields = rest.split(",", 3)
                if len(fields) == 4:
                    a, b = _parse_clock(fields[1].strip()), _parse_clock(fields[2].strip())
                    if self._inside(a, b):
                        return []
                    fields[1], fields[2] = _format_clock(self._remap(a), "ass"), _format_clock(self._remap(b), "ass")
                    return [head + ":" + ",".join(fields)]
            return [line]
        # SRT / WebVTT: block based. Timing lines decide whether the block survives.
        if self._dropping:
            if not line.strip():
                self._dropping = False
            return []
        m = _SRT_VTT_CUE.match(line.strip())
        if m:
            a, b = _parse_clock(m.group("a")), _parse_clock(m.group("b"))
            held, self._held = self._held, None
            if self._inside(a, b):
                self._dropping = True
                return []
            timing = f"{_format_clock(self._remap(a), self._style)} --> {_format_clock(self._remap(b), self._style)}{m.group('rest')}"
            return ([held] if held is not None else []) + [timing]
        out = [self._held] if self._held is not None else []
        self._held = None
        if self._style == "srt" and line.strip().isdigit():
            self._held = line  # a cue number: wait for its timing line
            return out
        return out + [line]

    def feed(self, text: str) -> str:
        if not self.spans:
            return text
        self._buf += text.replace("\r\n", "\n")
        *lines, self._buf = self._buf.split("\n")
        return "".join(line + "\n" for out in lines for line in self._line(out))

    def close(self) -> str:
        if not self.spans:
            return ""
        tail = "".join(line + "\n" for line in self._line(self._buf)) if self._buf else ""
        self._buf = ""
        if self._held is not None:
            tail += self._held + "\n"
            self._held = None
        return tail


def shift_subtitles(text: str, spans: list[Range], offset_ms: int = 0) -> str:
    """Re-time a whole subtitle file (see SubtitleShifter)."""
    if not spans:
        return text
    shifter = SubtitleShifter(spans, offset_ms)
    out = shifter.feed(text) + shifter.close()
    # Blocks whose cue was dropped leave consecutive blank lines behind; collapse them.
    return re.sub(r"\n{3,}", "\n\n", out).lstrip("\n")


class SessionStore:
    """Durable store of proxy sessions; production uses the Cleanplex database."""

    async def save(self, session_id: str, data: dict) -> None:
        from . import database as db

        await db.save_proxy_session(session_id, data)

    async def load(self, session_id: str) -> dict | None:
        from . import database as db

        return await db.get_proxy_session(session_id)


async def db_part_lookup(part_id: int) -> str | None:
    from . import database as db

    return await db.get_rating_key_for_part(part_id)


@dataclass
class _Session:
    rating_key: str
    offset_ms: int
    spans: list[Range] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)  # segment indexes the client must never be served
    ranges: list[Range] = field(default_factory=list)  # what to cut, decided when the stream started
    username: str | None = None
    client_id: str = ""
    client_title: str = ""
    # First kept segment index after each cut -> index of that cut in `spans`; the client
    # asking for it is the moment the viewer has played across the cut (see record_cut_played).
    after_cut: dict[int, int] = field(default_factory=dict)
    recorded: set[int] = field(default_factory=set)  # cut indexes already recorded
    keyframes: list[int] | None = None  # set when the stream is a copy (see edit_playlist)


RangesProvider = Callable[[str, str | None], Awaitable[list[Range]]]  # (rating_key, username) -> ranges


@dataclass
class Decision:
    """Whether a playback request gets its stream edited, and why."""

    inject: bool
    reason: str
    ranges: list[Range] = field(default_factory=list)
    username: str | None = None


@dataclass
class Policy:
    """The three conditions that must all hold before a stream is forced to transcode and edited.

    1. The client cannot be skipped live (client_controllable says it can't).
    2. The viewer has filtering turned on (filter_enabled).
    3. The title has segments that viewer's settings would actually skip (ranges_for is non-empty).

    A check left as None is not applied, which is how the standalone --cuts mode edits everything.
    Checks run cheapest first, and a failed one stops the rest: a client the live skip already
    reaches never costs a plex.tv lookup or a database read.
    """

    ranges_for: RangesProvider
    client_controllable: Callable[[str, str], Awaitable[bool]] | None = None  # (client_identifier, remote_ip)
    resolve_user: Callable[[str], Awaitable[str | None]] | None = None  # plex token -> username
    filter_enabled: Callable[[str], Awaitable[bool]] | None = None  # username
    # Filtering is the safe default: a viewer who cannot be identified is treated as filtered.
    unknown_user_filtered: bool = True
    closers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)


def cut_boundaries(removed: list[int]) -> dict[int, int]:
    """Map the segment index that follows each run of removed segments to the run's number (0, 1, ...)."""
    out: dict[int, int] = {}
    run = 0
    for i, index in enumerate(removed):
        if i + 1 == len(removed) or removed[i + 1] != index + 1:
            out[index + 1] = run
            run += 1
    return out


async def record_cut_played(session: "_Session", span: Range) -> int:
    """Record a skip event for each segment of the title inside *span* that this viewer's settings skip.

    The proxy removes time, not segments, so the segments are looked up by overlap. Called
    when the client fetches the first segment after the cut, i.e. when the viewer really
    passed it. Returns how many events were recorded.
    """
    from . import database as db
    from . import events
    from .filter_engine import _is_filtered, _resolve_action

    segments = await db.get_active_segments_for_rating_key(session.rating_key)
    prefs = await db.get_user_category_prefs(session.username) if session.username else {}
    recorded = 0
    for seg in segments:
        if seg["end_ms"] <= span[0] or seg["start_ms"] >= span[1]:
            continue
        if not _is_filtered(seg, prefs) or _resolve_action(seg, prefs) != "skip":
            continue
        await db.record_skip_event(
            plex_guid=seg["plex_guid"], title=seg.get("title", ""), username=session.username or "",
            client_identifier=session.client_id, client_title=session.client_title,
            category=seg.get("category", ""), action="skip", segment_id=seg["id"],
            position_ms=span[0], target_ms=span[1], success=True, latency_ms=0,
        )
        events.publish("skip_recorded", guid=seg["plex_guid"])
        recorded += 1
    return recorded


# Products live skipping can never reach: a browser exposes no control port and the server
# answers its player commands with 404 (verified on Plex Web 4.162, 2026-11-05). Waiting to
# learn that from a failed skip would let the first viewing play the segment.
NEVER_CONTROLLABLE_PRODUCTS = {"Plex Web"}


async def decide(
    policy: Policy, rating_key: str, client_id: str, remote_ip: str, token: str, product: str = ""
) -> Decision:
    """Apply the policy's conditions in order and return the outcome with the reason."""
    if (
        product not in NEVER_CONTROLLABLE_PRODUCTS
        and policy.client_controllable
        and await policy.client_controllable(client_id, remote_ip)
    ):
        return Decision(False, "live skipping reaches this client")
    username = await policy.resolve_user(token) if policy.resolve_user and token else None
    if policy.filter_enabled:
        if username is None and not policy.unknown_user_filtered:
            return Decision(False, "viewer could not be identified")
        if username is not None and not await policy.filter_enabled(username):
            return Decision(False, f"filtering is off for {username}", username=username)
    ranges = await policy.ranges_for(rating_key, username)
    if not ranges:
        return Decision(False, "no eligible segments for this title", username=username)
    return Decision(True, f"{len(ranges)} range(s) to cut", ranges, username)


def static_ranges(ranges: list[Range]) -> RangesProvider:
    """Provider that applies the same ranges to every title (for testing on any file)."""

    async def provider(_rating_key: str, _username: str | None) -> list[Range]:
        return ranges

    return provider


def eligible_ranges(pre_ms: int = 3000, post_ms: int = 3000) -> RangesProvider:
    """Provider of the ranges this viewer's settings would skip, widened by the usual buffers.

    A segment counts when the viewer's category level makes it fire and the action that
    results (their override, else the segment's own) is skip, exactly the live-skip rule.
    """
    from . import database as db
    from .filter_engine import _is_filtered, _resolve_action

    async def provider(rating_key: str, username: str | None) -> list[Range]:
        segments = await db.get_active_segments_for_rating_key(rating_key)
        prefs = await db.get_user_category_prefs(username) if username else {}
        # A playlist cannot lower the volume: mute segments are either cut or left alone (#184).
        cut_mutes = (await db.get_setting("enforcement_mute_mode", "leave")) == "cut"
        actions = {"skip", "mute"} if cut_mutes else {"skip"}
        return [
            (max(0, seg["start_ms"] - pre_ms), seg["end_ms"] + post_ms)
            for seg in segments
            if _is_filtered(seg, prefs) and _resolve_action(seg, prefs) in actions
        ]

    return provider


def is_lan_address(host: str) -> bool:
    """True for a private, loopback or link-local address, i.e. a client on the server's network."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


async def lan_client_controllable(client_id: str, remote_ip: str) -> bool:
    """Whether live skipping reaches this client.

    The session watcher records the truth per client (a player on the server's network
    that reports state). A client it has never seen is guessed from its address: local
    clients are assumed reachable, so they are left alone until the watcher learns better.
    """
    from . import database as db

    try:
        known = await db.get_client_control(client_id) if client_id else None
    except Exception as exc:
        # A database that predates the registry (or is being migrated) must not break playback.
        logger.warning("Client registry unavailable, guessing from the address: %s", exc)
        known = None
    return known if known is not None else is_lan_address(remote_ip)


async def db_filter_enabled(username: str) -> bool:
    """Filtering is on unless the user has an explicit record turning it off."""
    from . import database as db

    record = await db.get_user_filter(username)
    return record is None or bool(record["enabled"])


class PlexTvUsers:
    """Resolve a Plex token to the account name Cleanplex keys its user settings by."""

    TTL_S = 600  # a token's owner does not change; this only bounds a revoked token's lifetime
    FAIL_TTL_S = 60  # a failed lookup is not retried on every request (plex.tv slow or down)

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        # Short timeout: playback start waits on this, and an unknown viewer is treated as filtered.
        self._client = httpx.AsyncClient(base_url="https://plex.tv", transport=transport, timeout=3)
        self._cache: dict[str, tuple[float, str | None]] = {}

    async def __call__(self, token: str) -> str | None:
        cached = self._cache.get(token)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        try:
            resp = await self._client.get(
                "/api/v2/user", headers={"X-Plex-Token": token, "Accept": "application/json",
                                         "X-Plex-Client-Identifier": "cleanplex-stream-proxy"},
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Could not identify the Plex user for a token: %s", exc)
            self._cache[token] = (time.monotonic() + self.FAIL_TTL_S, None)
            return None
        # Managed users have no username; their title is what Plex shows (and what sessions report).
        name = data.get("username") or data.get("title") or None
        if name:
            self._cache[token] = (time.monotonic() + self.TTL_S, name)
        return name

    async def aclose(self) -> None:
        await self._client.aclose()


async def db_keyframes(rating_key: str) -> list[int] | None:
    """Keyframes of a title's single media file, if indexed and unchanged; None means re-encode."""
    from . import database as db
    from .keyframe_index import keyframes_for

    # Copy mode only for a title with exactly one file: with several versions Plex may play a
    # different one than the file that was indexed, and its keyframes would not match.
    if not rating_key or await db.count_parts_for_rating_key(rating_key) != 1:
        return None
    path = await db.get_file_for_rating_key(rating_key)
    return (await keyframes_for(path) or None) if path else None


def db_policy(
    pre_ms: int = 3000, post_ms: int = 3000, unknown_user_filtered: bool = True, ignore_client: bool = True
) -> Policy:
    """The production policy: filter switch and the viewer's own levels, for every client.

    Every client is edited by default (#184): since Plex's 2026 apps, phones, Apple TV and
    browsers accept no remote commands, so "live skipping reaches this client" is not a safe
    reason to leave a stream alone. ignore_client=False restores that check.
    """
    users = PlexTvUsers()
    return Policy(
        ranges_for=eligible_ranges(pre_ms, post_ms),
        client_controllable=None if ignore_client else lan_client_controllable,
        resolve_user=users,
        filter_enabled=db_filter_enabled,
        unknown_user_filtered=unknown_user_filtered,
        closers=[users.aclose],
    )


def create_proxy_app(
    plex_url: str,
    policy: Policy,
    token: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
    trust_forwarded_for: bool = False,
    ws_connect=None,
    keyframes_provider: Callable[[str], Awaitable[list[int] | None]] | None = None,
    session_store: "SessionStore | None" = None,
    part_lookup: Callable[[int], Awaitable[str | None]] | None = None,
) -> FastAPI:
    """Build the proxy app. *token* is added to requests that carry none (local testing only).

    *trust_forwarded_for* takes the client address from X-Forwarded-For; enable it only
    behind a proxy you control, since the header is otherwise client-supplied. *ws_connect*
    opens the upstream WebSocket (defaults to the websockets library); tests inject a fake.
    """
    state: dict = {
        "sessions": {},  # Plex transcode session id -> _Session
        "decisions": OrderedDict(),  # session id -> Decision, bounded
        "by_client": {},  # (client identifier, rating key) -> transcode session id, for progress reports
        "requests": deque(maxlen=5000),  # segment fetches: what the client really asked for
        "telemetry": deque(maxlen=2000),  # reports posted by the test page
        "part_decisions": {},  # (token, rating key) -> (expires, Decision)
    }

    async def _get_session(session_id: str) -> "_Session | None":
        """The edit state of a transcode session, reloaded from the store after a restart."""
        found = state["sessions"].get(session_id)
        if found is not None or not session_store or not session_id:
            return found
        stored = await session_store.load(session_id)
        if not stored:
            return None
        found = _Session(
            stored["rating_key"], stored["offset_ms"], spans=stored["spans"], removed=stored["removed"],
            ranges=stored["ranges"], username=stored["username"] or None, client_id=stored["client_id"],
            client_title=stored["client_title"],
            keyframes=(await keyframes_provider(stored["rating_key"])) if keyframes_provider else None,
        )
        found.after_cut = cut_boundaries(found.removed)
        state["sessions"][session_id] = found
        return found

    async def _save_session(session_id: str, sess: "_Session") -> None:
        if session_store:
            try:
                await session_store.save(session_id, {
                    "rating_key": sess.rating_key, "username": sess.username, "client_id": sess.client_id,
                    "client_title": sess.client_title, "offset_ms": sess.offset_ms, "ranges": sess.ranges,
                    "spans": sess.spans, "removed": sess.removed,
                })
            except Exception as exc:  # persistence must never break playback
                logger.warning("Could not store proxy session %s: %s", session_id, exc)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # One client for the life of the app (CLAUDE.md 5.3); closed on shutdown.
        app.state.client = httpx.AsyncClient(base_url=plex_url, transport=transport, timeout=60)
        yield
        await app.state.client.aclose()
        for close in policy.closers:
            await close()

    app = FastAPI(lifespan=lifespan)
    app.state.proxy_state = state

    def _params(request: Request) -> dict[str, str]:
        params = dict(parse_qsl(request.url.query, keep_blank_values=True))
        if token and "X-Plex-Token" not in params and "x-plex-token" not in request.headers:
            params["X-Plex-Token"] = token
        return params

    def _forward_headers(request: Request) -> dict[str, str]:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP and k.lower() != "content-length"}
        # httpx would otherwise ask Plex for gzip on behalf of a client that did not,
        # and raw-streaming that back would hand the client a body it cannot read.
        headers.setdefault("accept-encoding", "identity")
        return headers

    def _playback_ids(request: Request, params: dict[str, str]) -> tuple[str, str]:
        """(session id, rating key) of a playback request; blank when it names neither."""
        match = _RATING_KEY.search(params.get("path", ""))
        session_id = params.get("session") or request.headers.get("x-plex-session-identifier", "")
        return session_id, (match.group(1) if match else "")

    def _remote_ip(request: Request) -> str:
        if trust_forwarded_for and request.headers.get("x-forwarded-for"):
            return request.headers["x-forwarded-for"].split(",")[0].strip()
        return request.client.host if request.client else ""

    async def _decide_playback(request: Request, params: dict[str, str]) -> Decision | None:
        """The decision for this playback session, made once (on /decision or /start) and reused."""
        session_id, rating_key = _playback_ids(request, params)
        if not session_id or not rating_key:
            return None
        known = state["decisions"].get(session_id)
        if known:
            return known
        client_id = params.get("X-Plex-Client-Identifier") or request.headers.get("x-plex-client-identifier", "")
        plex_token = params.get("X-Plex-Token") or request.headers.get("x-plex-token", "")
        product = params.get("X-Plex-Product") or request.headers.get("x-plex-product", "")
        decision = await decide(policy, rating_key, client_id, _remote_ip(request), plex_token, product)
        logger.info("Stream decision for session %s (rating key %s, client %s): %s: %s",
                    session_id, rating_key, client_id or "?", "EDIT" if decision.inject else "leave alone", decision.reason)
        state["decisions"][session_id] = decision
        while len(state["decisions"]) > 2000:
            state["decisions"].popitem(last=False)
        return decision

    def _remap_progress(request: Request, params: dict[str, str]) -> None:
        """Translate a progress report from the shortened stream's time back to the film's.

        The player reports offset + elapsed in the edited stream; Plex must store the real
        position or resume, history and "watched" would all land too early. The session is
        found by its transcode session id, else by (client, title).
        """
        raw = params.get("time")
        if raw is None or not str(raw).lstrip("-").isdigit():
            return
        session_id = request.headers.get("x-plex-session-identifier") or params.get("X-Plex-Session-Identifier", "")
        session = state["sessions"].get(session_id)
        if session is None:
            client_id = params.get("X-Plex-Client-Identifier") or request.headers.get("x-plex-client-identifier", "")
            rating_key = params.get("ratingKey", "")
            if not rating_key and params.get("key"):
                match = _RATING_KEY.search(params["key"])
                rating_key = match.group(1) if match else ""
            found = state["by_client"].get((client_id, rating_key))
            session = state["sessions"].get(found) if found else None
        if session is None or not session.spans:
            return
        params["time"] = str(edited_to_original(int(raw), session.spans))

    async def _refuse_part(part_id: int, request: Request, params: dict[str, str]) -> bool:
        """True when a direct file request must be refused: its title is flagged for this viewer (#184)."""
        if not part_lookup:
            return False
        rating_key = await part_lookup(part_id)
        if not rating_key:
            return False
        plex_token = params.get("X-Plex-Token") or request.headers.get("x-plex-token", "")
        key = (plex_token, rating_key)
        cached = state["part_decisions"].get(key)
        if cached and cached[0] > time.monotonic():
            decision = cached[1]
        else:
            client_id = params.get("X-Plex-Client-Identifier") or request.headers.get("x-plex-client-identifier", "")
            product = params.get("X-Plex-Product") or request.headers.get("x-plex-product", "")
            decision = await decide(policy, rating_key, client_id, _remote_ip(request), plex_token, product)
            state["part_decisions"][key] = (time.monotonic() + _PART_DECISION_TTL_S, decision)
        if decision.inject:
            # Fail closed: the original file of a flagged title cannot be edited.
            logger.info("Refused direct file request for rating key %s: %s", rating_key, decision.reason)
        return decision.inject

    async def _note_segment(session_id: str, index: int) -> None:
        """Record a played cut when the client asks for the first segment after it."""
        state["requests"].append({"session": session_id, "segment": index})
        played = await _get_session(session_id)
        cut = played.after_cut.get(index) if played else None
        if played and cut is not None and cut not in played.recorded and cut < len(played.spans):
            played.recorded.add(cut)
            try:
                await record_cut_played(played, played.spans[cut])
            except Exception as exc:  # recording is bookkeeping; it must never break playback
                logger.warning("Could not record a skip for session %s: %s", session_id, exc)

    @app.get("/_proxy/authorize")
    async def authorize(request: Request) -> Response:
        """nginx auth_request for direct file requests: 204 to serve the bytes from Plex, 403 to refuse.

        nginx streams the file itself; only this yes/no reaches Python (X-Original-URI).
        """
        original = request.headers.get("x-original-uri", "")
        path, _, query = original.partition("?")
        part = _PART.match(path)
        params = dict(parse_qsl(query, keep_blank_values=True))
        if part and await _refuse_part(int(part.group(1)), request, params):
            return Response(status_code=403)
        return Response(status_code=204)

    @app.get("/_proxy/segment")
    async def segment_seen(request: Request) -> Response:
        """nginx auth_request for HLS segments: records played cuts and refuses removed ones.

        A removed segment is not in the edited playlist, but a player that builds segment URLs
        itself would otherwise get it straight from Plex (#184). Bytes come from Plex either way.
        """
        seg = _SEGMENT.match(request.headers.get("x-original-uri", "").partition("?")[0])
        if seg:
            sess = await _get_session(seg.group(1))
            if sess and int(seg.group(2)) in set(sess.removed):
                logger.info("Refused removed segment %s of session %s", seg.group(2), seg.group(1))
                return Response(status_code=403)
            await _note_segment(seg.group(1), int(seg.group(2)))
        return Response(status_code=204)

    @app.get("/_proxy/report")
    async def report() -> JSONResponse:
        """Segment fetches and test-page telemetry recorded so far, for verifying a run."""
        sessions = {
            sid: {"rating_key": s.rating_key, "offset_ms": s.offset_ms, "spans": s.spans, "removed": s.removed}
            for sid, s in state["sessions"].items()
        }
        decisions = {sid: {"inject": d.inject, "reason": d.reason, "username": d.username}
                     for sid, d in state["decisions"].items()}
        return JSONResponse(
            {"requests": list(state["requests"]), "telemetry": list(state["telemetry"]),
             "sessions": sessions, "decisions": decisions}
        )

    @app.get("/_proxy/plan")
    async def plan(session: str) -> JSONResponse:
        """The cut spans (original timeline, ms) applied to a session, for the test page's scripted run."""
        found = state["sessions"].get(session)
        return JSONResponse({"spans": found.spans if found else []})

    @app.post("/_proxy/telemetry")
    async def telemetry(request: Request) -> JSONResponse:
        """Record one event reported by the test page."""
        state["telemetry"].append(await request.json())
        return JSONResponse({"ok": True})

    @app.get("/_proxy/test", response_class=HTMLResponse)
    async def test_page(rating_key: str, offset: int = 0, scenario: int = 0, marks: str = "") -> HTMLResponse:
        """Serve a page that plays the title through this proxy and reports what happens."""
        page = TEST_PAGE.replace("__RATING_KEY__", rating_key).replace("__OFFSET__", str(offset))
        page = page.replace("__MARKS__", ",".join(m for m in marks.split(",") if m.strip().isdigit()))
        return HTMLResponse(page.replace("__SCENARIO__", "true" if scenario else "false"))

    @app.websocket("/{path:path}")
    async def proxy_websocket(websocket: WebSocket, path: str) -> None:
        """Pass Plex's notification socket through, so apps that rely on it keep working."""
        await websocket.accept(subprotocol=(websocket.scope.get("subprotocols") or [None])[0])
        query = websocket.url.query
        upstream_url = plex_url.replace("http", "ws", 1).rstrip("/") + "/" + path + (f"?{query}" if query else "")
        connect = ws_connect or _open_upstream_ws
        try:
            async with connect(upstream_url) as upstream:

                async def down() -> None:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)

                async def up() -> None:
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        if message.get("bytes") is not None:
                            await upstream.send(message["bytes"])
                        elif message.get("text") is not None:
                            await upstream.send(message["text"])

                tasks = [asyncio.create_task(down()), asyncio.create_task(up())]
                _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
        except Exception as exc:
            logger.debug("WebSocket proxy to %s ended: %s", upstream_url.split("?")[0], exc)
        finally:
            try:
                await websocket.close()
            except RuntimeError:
                pass  # already closed by the client

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"])
    async def proxy(path: str, request: Request) -> Response:
        """Forward to Plex, forcing HLS transcode on playback requests and editing variant playlists."""
        client: httpx.AsyncClient = request.app.state.client
        full_path = "/" + path
        params = _params(request)

        part = _PART.match(full_path)
        if part and await _refuse_part(int(part.group(1)), request, params):
            return Response("This title is filtered for this viewer; play it again to get the filtered stream.",
                            status_code=403)

        if full_path in _START_PATHS or full_path == _DECISION_PATH:
            decision = await _decide_playback(request, params)
            if decision and decision.inject and (
                params.get("protocol", "hls") != "hls" or full_path.endswith(".mpd")
            ):
                # Only HLS playlists are edited; any other stream of a flagged title is refused (#184).
                logger.info("Refused %s stream for a filtered title", params.get("protocol") or "dash")
                return Response("This title is filtered for this viewer and needs an HLS stream.", status_code=403)
            if decision and decision.inject:
                # Direct play would bypass the edit entirely, so take it off the table. With the
                # file's keyframes known the video is copied, keeping 4K/HDR intact; otherwise it is
                # re-encoded, whose segments fall on an exact grid.
                _, rk = _playback_ids(request, params)
                keyframes = await keyframes_provider(rk) if keyframes_provider else None
                params.update({"directPlay": "0", "directStream": "1" if keyframes else "0"})
                if full_path in _START_PATHS:
                    session_id, rating_key = _playback_ids(request, params)
                    offset_ms = int(float(params.get("offset", "0") or 0) * 1000)
                    client_id = params.get("X-Plex-Client-Identifier") or request.headers.get("x-plex-client-identifier", "")
                    state["sessions"][session_id] = _Session(
                        rating_key, offset_ms, ranges=decision.ranges, username=decision.username, client_id=client_id,
                        client_title=params.get("X-Plex-Device-Name") or params.get("X-Plex-Product", ""),
                        keyframes=keyframes or None,
                    )
                    if client_id:
                        state["by_client"][(client_id, rating_key)] = session_id
                    await _save_session(session_id, state["sessions"][session_id])

        if full_path in _PROGRESS_PATHS:
            _remap_progress(request, params)

        if full_path == _SUBTITLES_PATH:
            sid = params.get("session") or request.headers.get("x-plex-session-identifier", "")
            sub_session = await _get_session(sid)
            upstream = await client.send(
                client.build_request("GET", full_path, params=params, headers=_forward_headers(request)), stream=True
            )
            if sub_session is None or upstream.status_code != 200:
                return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                                         headers=_response_headers(upstream, decoded=False),
                                         background=BackgroundTask(upstream.aclose))
            if not sub_session.spans:
                # Subtitles can be asked for before the playlist; work out the cuts from it now.
                pl = await client.get(f"/video/:/transcode/universal/session/{sid}/base/index.m3u8",
                                      params={k: v for k, v in params.items() if k.lower().startswith("x-plex")},
                                      headers=_forward_headers(request))
                if pl.status_code == 200:
                    edit = edit_playlist(pl.text, sub_session.ranges, sub_session.offset_ms, sub_session.keyframes)
                    sub_session.spans, sub_session.removed = edit.spans, edit.removed
                    sub_session.after_cut = cut_boundaries(edit.removed)
                    await _save_session(sid, sub_session)
            shifter = SubtitleShifter(sub_session.spans, sub_session.offset_ms)
            logger.info("Re-timing subtitles for session %s across %d cut(s)", sid, len(sub_session.spans))

            async def retimed():
                async for chunk in upstream.aiter_text():
                    out = shifter.feed(chunk)
                    if out:
                        yield out.encode()
                tail = shifter.close()
                if tail:
                    yield tail.encode()

            return StreamingResponse(retimed(), status_code=200, headers=_response_headers(upstream),
                                     background=BackgroundTask(upstream.aclose))

        variant = _VARIANT.match(full_path)
        if variant:
            # Some players (desktop WebKit) ask for the playlist with a Range header; Plex
            # answers 206 with a slice we cannot edit. A server may ignore Range, so fetch
            # and edit the whole playlist and answer 200.
            headers = {k: v for k, v in _forward_headers(request).items() if k.lower() not in ("range", "if-range")}
            upstream = await client.get(full_path, params=params, headers=headers)
            session = await _get_session(variant.group(1))
            if upstream.status_code != 200 or session is None:
                return Response(upstream.content, upstream.status_code, _response_headers(upstream))
            edit = edit_playlist(upstream.text, session.ranges, session.offset_ms, session.keyframes)
            session.spans = edit.spans
            session.removed = edit.removed
            session.after_cut = cut_boundaries(edit.removed)
            await _save_session(variant.group(1), session)
            logger.info("Edited playlist for session %s: removed %d segment(s), %d ms",
                        variant.group(1), len(edit.removed), edit.removed_ms)
            return Response(edit.text, 200, _response_headers(upstream), media_type="application/vnd.apple.mpegurl")

        segment = _SEGMENT.match(full_path)
        if segment:
            await _note_segment(segment.group(1), int(segment.group(2)))

        body = await request.body()
        upstream_request = client.build_request(
            request.method, full_path, params=params, headers=_forward_headers(request), content=body or None
        )
        upstream = await client.send(upstream_request, stream=True)
        if upstream.is_stream_consumed:
            # Some transports (the test mock) hand back an already-read body.
            return Response(upstream.content, upstream.status_code, _response_headers(upstream))
        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=_response_headers(upstream, decoded=False),
            background=BackgroundTask(upstream.aclose),
        )

    return app


def _open_upstream_ws(url: str):
    """Connect to Plex's WebSocket with the websockets library (a context manager)."""
    import websockets

    return websockets.connect(url, max_size=None)


def _response_headers(upstream: httpx.Response, decoded: bool = True) -> dict[str, str]:
    """Upstream headers minus hop-by-hop ones.

    decoded=True is for a body we re-read (httpx has already decompressed it), so its
    encoding and length no longer describe what we send. decoded=False is for raw
    streaming, where both stay correct.
    """
    # date/server: the ASGI server adds its own, and nginx warns about duplicates.
    skip = _HOP_BY_HOP | {"date", "server"} | ({"content-encoding", "content-length"} if decoded else set())
    return {k: v for k, v in upstream.headers.items() if k.lower() not in skip}


TEST_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cleanplex stream proxy test</title>
<style>body{font:14px -apple-system,sans-serif;margin:12px;background:#111;color:#ddd}video{width:100%;background:#000}pre{white-space:pre-wrap;font-size:11px}</style>
</head><body>
<video id="v" controls muted playsinline></video>
<pre id="log">starting</pre>
<script src="https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.13/hls.min.js"></script>
<script>
const v = document.getElementById('v'), logEl = document.getElementById('log');
const sid = Math.random().toString(16).slice(2, 18);
const q = new URLSearchParams({
  path: '/library/metadata/__RATING_KEY__', mediaIndex: 0, partIndex: 0, protocol: 'hls', offset: __OFFSET__,
  fastSeek: 1, directPlay: 1, directStream: 1, maxVideoBitrate: 4000, videoQuality: 100, session: sid,
  'X-Plex-Client-Identifier': 'cleanplex-proxy-test-' + sid, 'X-Plex-Product': 'CleanplexProxyTest',
  'X-Plex-Platform': navigator.userAgent.includes('Safari') && !navigator.userAgent.includes('Chrome') ? 'iOS' : 'Chrome'
});
const url = '/video/:/transcode/universal/start.m3u8?' + q;
const events = [];
function note(type, extra) {
  const e = Object.assign({type, ms: Date.now(), t: +v.currentTime.toFixed(2), dur: isFinite(v.duration) ? +v.duration.toFixed(2) : null, ua: navigator.userAgent.slice(0, 60)}, extra || {});
  events.push(e); logEl.textContent = events.slice(-12).map(x => JSON.stringify(x)).join('\\n');
  fetch('/_proxy/telemetry', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(e)}).catch(() => {});
}
['loadedmetadata', 'playing', 'waiting', 'stalled', 'seeking', 'seeked', 'ended', 'error'].forEach(n =>
  v.addEventListener(n, () => note(n, n === 'error' && v.error ? {code: v.error.code, msg: v.error.message} : {})));
let last = -1;
v.addEventListener('timeupdate', () => { const s = Math.floor(v.currentTime); if (s !== last) { last = s; note('tick'); } });
// Optional scripted run (?scenario=1), in the shortened stream's own time: play, jump to just before
// a cut and play across it, jump back across the first cut, then jump near the end.
const SCENARIO = __SCENARIO__;
const MARKS = '__MARKS__'.split(',').filter(Boolean).map(Number);  // control run: original-time cut starts to probe without cutting
async function scenario() {
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const until = (cond, ms) => new Promise(r => { const t0 = Date.now(); const i = setInterval(() => { if (cond() || Date.now() - t0 > ms) { clearInterval(i); r(); } }, 250); });
  await until(() => v.currentTime > 6, 60000);
  let spans = [];
  for (let i = 0; i < 20 && !spans.length; i++) { spans = (await (await fetch('/_proxy/plan?session=' + sid)).json()).spans; if (!spans.length) await wait(500); }
  if (!spans.length) spans = MARKS.map(m => [m * 1000, m * 1000]);  // control: same positions, nothing removed
  note('plan', {cuts: spans.length, removed_s: spans.reduce((a, [s, e]) => a + (e - s) / 1000, 0)});
  // Where a cut sits in the shortened stream: its original start minus everything removed before it.
  const edited = i => spans[i][0] / 1000 - spans.slice(0, i).reduce((a, [s, e]) => a + (e - s) / 1000, 0);
  const steps = [];
  for (const i of [0, Math.floor(spans.length / 2), spans.length - 1]) if (i < spans.length) steps.push(['play-across-cut-' + i, Math.max(0, edited(i) - 8), 22000]);
  steps.push(['jump-back-before-cut-0', Math.max(0, edited(0) - 8), 20000]);
  steps.push(['jump-near-end', v.duration - 6, 12000]);
  for (const [name, to, playFor] of steps) { note('step', {step: name, to: +to.toFixed(1)}); v.currentTime = to; await wait(playFor); }
  note('scenario-done');
}
// Plex refuses to start a transcode for a session that has no decision, so ask first, as its apps do.
async function begin() {
  const d = await fetch('/video/:/transcode/universal/decision?' + q, {headers: {Accept: 'application/json'}});
  note('decision', {status: d.status});
  const forceHls = new URLSearchParams(location.search).get('engine') === 'hls';  // exercise the MSE path (hls.js) where native HLS exists
  if (!forceHls && v.canPlayType('application/vnd.apple.mpegurl')) { v.src = url; note('engine', {engine: 'native'}); }
  else if (window.Hls && Hls.isSupported()) { const h = new Hls(); h.loadSource(url); h.attachMedia(v); note('engine', {engine: 'hls.js'});
    h.on(Hls.Events.ERROR, (_, x) => note('hls-error', {fatal: x.fatal, details: x.details})); }
  else note('engine', {engine: 'none'});
  v.play().catch(e => note('play-rejected', {msg: String(e)}));
}
window.cleanplexSeek = t => { v.currentTime = t; };
begin().then(() => SCENARIO && scenario()).catch(e => note('begin-failed', {msg: String(e)}));
</script></body></html>
"""


def _lan_addresses() -> list[str]:
    """This machine's private IPv4 addresses, for the "open this URL" hint."""
    import socket

    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    return sorted({info[4][0] for info in infos if is_lan_address(info[4][0]) and not info[4][0].startswith("127.")})


def main() -> None:
    """Run the proxy standalone: python -m cleanplex.stream_proxy --cuts 60000-75000."""
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plex", default="http://127.0.0.1:32400")
    parser.add_argument("--port", type=int, default=7981)
    parser.add_argument("--cuts", default="", help="ms ranges applied to every title, e.g. 60000-75000,300000-310000")
    parser.add_argument("--unknown-user-unfiltered", action="store_true",
                        help="leave a viewer alone when their Plex token cannot be resolved (default: treat as filtered)")
    parser.add_argument("--force-inject", action="store_true",
                        help="testing: edit streams even for clients live skipping reaches (skips condition 1)")
    parser.add_argument("--no-migrate", action="store_true", help="do not run database migrations (use with a live database)")
    parser.add_argument("--trust-forwarded-for", action="store_true", help="take the client address from X-Forwarded-For")
    parser.add_argument("--db", default="", help="use each title's skip segments from this Cleanplex database instead of --cuts")
    parser.add_argument("--token", default=os.environ.get("PLEX_TOKEN", ""),
                        help="Plex token added to requests that carry none (default: $PLEX_TOKEN, keeps it out of ps)")
    args = parser.parse_args()
    if args.db:
        from pathlib import Path

        from . import database as db

        db.set_db_path(Path(args.db))
        if not args.no_migrate:
            asyncio.run(db.init_db())  # additive migrations: brings a copied database up to date
        policy = db_policy(unknown_user_filtered=not args.unknown_user_unfiltered, ignore_client=args.force_inject)
    else:
        policy = Policy(ranges_for=static_ranges(parse_cuts(args.cuts)))
    app = create_proxy_app(args.plex, policy, args.token, trust_forwarded_for=args.trust_forwarded_for)
    print(f"Stream proxy for {args.plex} on port {args.port}")
    for address in _lan_addresses():
        print(f"  Plex Web through the proxy: http://{address}:{args.port}/web")
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
