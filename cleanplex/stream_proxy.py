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
import os
import re
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from .logger import get_logger

logger = get_logger(__name__)

Range = tuple[int, int]  # (start_ms, end_ms) in the original timeline

_EXTINF = re.compile(r"#EXTINF:([\d.]+)")
_RATING_KEY = re.compile(r"/library/metadata/(\d+)")
_START_PATHS = {"/video/:/transcode/universal/start.m3u8", "/video/:/transcode/universal/start"}
_DECISION_PATH = "/video/:/transcode/universal/decision"
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


RangesProvider = Callable[[str], Awaitable[list[Range]]]


def static_ranges(ranges: list[Range]) -> RangesProvider:
    """Provider that applies the same ranges to every title (for testing on any file)."""

    async def provider(_rating_key: str) -> list[Range]:
        return ranges

    return provider


def db_ranges(pre_ms: int = 3000, post_ms: int = 3000) -> RangesProvider:
    """Provider that cuts a title's active skip segments, widened by the usual buffers."""
    from . import database as db

    async def provider(rating_key: str) -> list[Range]:
        return [(max(0, start - pre_ms), end + post_ms) for start, end in await db.get_skip_ranges_for_rating_key(rating_key)]

    return provider


def create_proxy_app(
    plex_url: str,
    ranges_for: RangesProvider,
    token: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the proxy app. *token* is added to requests that carry none (local testing only)."""
    state: dict = {
        "sessions": {},  # Plex transcode session id -> _Session
        "requests": deque(maxlen=5000),  # segment fetches: what the client really asked for
        "telemetry": deque(maxlen=2000),  # reports posted by the test page
    }

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # One client for the life of the app (CLAUDE.md 5.3); closed on shutdown.
        app.state.client = httpx.AsyncClient(base_url=plex_url, transport=transport, timeout=60)
        yield
        await app.state.client.aclose()

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

    @app.get("/_proxy/report")
    async def report() -> JSONResponse:
        """Segment fetches and test-page telemetry recorded so far, for verifying a run."""
        sessions = {
            sid: {"rating_key": s.rating_key, "offset_ms": s.offset_ms, "spans": s.spans, "removed": s.removed}
            for sid, s in state["sessions"].items()
        }
        return JSONResponse(
            {"requests": list(state["requests"]), "telemetry": list(state["telemetry"]), "sessions": sessions}
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

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"])
    async def proxy(path: str, request: Request) -> Response:
        """Forward to Plex, forcing HLS transcode on playback requests and editing variant playlists."""
        client: httpx.AsyncClient = request.app.state.client
        full_path = "/" + path
        params = _params(request)

        if full_path in _START_PATHS or full_path == _DECISION_PATH:
            # Direct play would bypass the edit entirely, so take it off the table.
            params.update({"directPlay": "0", "directStream": "0"})
            if full_path in _START_PATHS:
                match = _RATING_KEY.search(params.get("path", ""))
                session_id = params.get("session") or request.headers.get("x-plex-session-identifier", "")
                if match and session_id:
                    offset_ms = int(float(params.get("offset", "0") or 0) * 1000)
                    state["sessions"][session_id] = _Session(match.group(1), offset_ms)

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
            ranges = await ranges_for(session.rating_key)
            edit = edit_playlist(upstream.text, ranges, session.offset_ms)
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


def main() -> None:
    """Run the proxy standalone: python -m cleanplex.stream_proxy --cuts 60000-75000."""
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plex", default="http://127.0.0.1:32400")
    parser.add_argument("--port", type=int, default=7981)
    parser.add_argument("--cuts", default="", help="ms ranges applied to every title, e.g. 60000-75000,300000-310000")
    parser.add_argument("--db", default="", help="use each title's skip segments from this Cleanplex database instead of --cuts")
    parser.add_argument("--token", default=os.environ.get("PLEX_TOKEN", ""),
                        help="Plex token added to requests that carry none (default: $PLEX_TOKEN, keeps it out of ps)")
    args = parser.parse_args()
    if args.db:
        from pathlib import Path

        from . import database as db

        db.set_db_path(Path(args.db))
        provider = db_ranges()
    else:
        provider = static_ranges(parse_cuts(args.cuts))
    app = create_proxy_app(args.plex, provider, args.token)
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
