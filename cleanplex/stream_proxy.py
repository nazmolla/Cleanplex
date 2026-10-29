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
_START_PATHS = {"/video/:/transcode/universal/start.m3u8", "/video/:/transcode/universal/start"}
_DECISION_PATH = "/video/:/transcode/universal/decision"
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


def edit_playlist(text: str, ranges: list[Range], offset_ms: int = 0) -> PlaylistEdit:
    """Drop every segment that overlaps one of *ranges* from a Plex variant playlist.

    A segment's time is the running sum of EXTINF durations, starting at *offset_ms*
    (the `offset` the stream was started at). Whole segments are removed, so the cut
    always widens outward to segment boundaries and never leaves flagged frames in.
    #EXT-X-DISCONTINUITY is inserted where a gap was cut, since the next segment's
    timestamps jump.
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
        if any(start < r_end and end > r_start for r_start, r_end in ranges):
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


@dataclass
class _Session:
    rating_key: str
    offset_ms: int
    spans: list[Range] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)  # segment indexes the client must never be served
    ranges: list[Range] = field(default_factory=list)  # what to cut, decided when the stream started
    username: str | None = None


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


async def decide(policy: Policy, rating_key: str, client_id: str, remote_ip: str, token: str) -> Decision:
    """Apply the policy's conditions in order and return the outcome with the reason."""
    if policy.client_controllable and await policy.client_controllable(client_id, remote_ip):
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
        return [
            (max(0, seg["start_ms"] - pre_ms), seg["end_ms"] + post_ms)
            for seg in segments
            if _is_filtered(seg, prefs) and _resolve_action(seg, prefs) == "skip"
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

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(base_url="https://plex.tv", transport=transport, timeout=10)
        self._cache: dict[str, tuple[float, str]] = {}

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
            return None
        # Managed users have no username; their title is what Plex shows (and what sessions report).
        name = data.get("username") or data.get("title") or None
        if name:
            self._cache[token] = (time.monotonic() + self.TTL_S, name)
        return name

    async def aclose(self) -> None:
        await self._client.aclose()


def db_policy(
    pre_ms: int = 3000, post_ms: int = 3000, unknown_user_filtered: bool = True, ignore_client: bool = False
) -> Policy:
    """The production policy: live-skip clients, filter switch and the viewer's own levels.

    *ignore_client* drops the first condition so a client live skipping reaches (a local Apple TV)
    can still be put through the proxy, for testing.
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
    }

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
        decision = await decide(policy, rating_key, client_id, _remote_ip(request), plex_token)
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

        if full_path in _START_PATHS or full_path == _DECISION_PATH:
            decision = await _decide_playback(request, params)
            if decision and decision.inject:
                # Direct play would bypass the edit entirely, so take it off the table.
                params.update({"directPlay": "0", "directStream": "0"})
                if full_path in _START_PATHS:
                    session_id, rating_key = _playback_ids(request, params)
                    offset_ms = int(float(params.get("offset", "0") or 0) * 1000)
                    state["sessions"][session_id] = _Session(
                        rating_key, offset_ms, ranges=decision.ranges, username=decision.username
                    )
                    client_id = params.get("X-Plex-Client-Identifier") or request.headers.get("x-plex-client-identifier", "")
                    if client_id:
                        state["by_client"][(client_id, rating_key)] = session_id

        if full_path in _PROGRESS_PATHS:
            _remap_progress(request, params)

        variant = _VARIANT.match(full_path)
        if variant:
            # Some players (desktop WebKit) ask for the playlist with a Range header; Plex
            # answers 206 with a slice we cannot edit. A server may ignore Range, so fetch
            # and edit the whole playlist and answer 200.
            headers = {k: v for k, v in _forward_headers(request).items() if k.lower() not in ("range", "if-range")}
            upstream = await client.get(full_path, params=params, headers=headers)
            session = state["sessions"].get(variant.group(1))
            if upstream.status_code != 200 or session is None:
                return Response(upstream.content, upstream.status_code, _response_headers(upstream))
            edit = edit_playlist(upstream.text, session.ranges, session.offset_ms)
            session.spans = edit.spans
            session.removed = edit.removed
            logger.info("Edited playlist for session %s: removed %d segment(s), %d ms",
                        variant.group(1), len(edit.removed), edit.removed_ms)
            return Response(edit.text, 200, _response_headers(upstream), media_type="application/vnd.apple.mpegurl")

        segment = _SEGMENT.match(full_path)
        if segment:
            state["requests"].append({"session": segment.group(1), "segment": int(segment.group(2))})

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
    skip = _HOP_BY_HOP | ({"content-encoding", "content-length"} if decoded else set())
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
