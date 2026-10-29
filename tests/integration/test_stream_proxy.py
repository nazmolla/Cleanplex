"""The stream proxy prototype in front of a fake Plex (#132)."""

from __future__ import annotations

import httpx
import pytest

from cleanplex.stream_proxy import Policy, create_proxy_app, static_ranges

VARIANT = "\n".join(
    ["#EXTM3U", "#EXT-X-TARGETDURATION:5", "#EXT-X-MEDIA-SEQUENCE:0"]
    + [line for i in range(6) for line in (f"#EXTINF:5, nodesc", f"{i:05d}.ts")]
    + ["#EXT-X-ENDLIST"]
) + "\n"
SESSION = "abc123"
BASE = f"/video/:/transcode/universal/session/{SESSION}/base"


class FakePlex:
    """Records what reached Plex and answers like its HLS endpoints."""

    def __init__(self):
        self.seen: list[httpx.Request] = []
        self.variant_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        path = request.url.path
        if path == "/video/:/transcode/universal/start.m3u8":
            return httpx.Response(200, text=f"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nsession/{SESSION}/base/index.m3u8\n",
                                  headers={"content-type": "application/vnd.apple.mpegurl"})
        if path == f"{BASE}/index.m3u8":
            return httpx.Response(self.variant_status, text=VARIANT,
                                  headers={"content-type": "application/vnd.apple.mpegurl"})
        if path.endswith(".ts"):
            return httpx.Response(200, content=b"\x47" * 188, headers={"content-type": "video/MP2T"})
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def plex():
    return FakePlex()


@pytest.fixture
async def proxy(plex):
    app = create_proxy_app("http://plex.test", Policy(ranges_for=static_ranges([(7_000, 12_000)])),
                           token="TESTTOKEN", transport=httpx.MockTransport(plex))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as client:
            yield client, app


async def _start(client, **extra):
    params = {"path": "/library/metadata/42", "session": SESSION, "directPlay": "1", "directStream": "1",
              "protocol": "hls", **extra}
    return await client.get("/video/:/transcode/universal/start.m3u8", params=params)


async def test_start_forces_a_transcode_and_adds_the_token(proxy, plex):
    client, _ = proxy
    resp = await _start(client)
    assert resp.status_code == 200
    upstream = plex.seen[-1].url.params
    assert (upstream["directPlay"], upstream["directStream"]) == ("0", "0")
    assert upstream["X-Plex-Token"] == "TESTTOKEN"


async def test_a_client_supplied_token_is_not_replaced(proxy, plex):
    client, _ = proxy
    await _start(client, **{"X-Plex-Token": "CLIENTTOKEN"})
    assert plex.seen[-1].url.params["X-Plex-Token"] == "CLIENTTOKEN"


async def test_the_decision_request_is_forced_too(proxy, plex):
    client, _ = proxy
    await client.get("/video/:/transcode/universal/decision", params={
        "path": "/library/metadata/42", "session": SESSION, "directPlay": "1", "directStream": "1"})
    assert plex.seen[-1].url.params["directPlay"] == "0"


async def test_variant_playlist_is_edited_for_the_started_session(proxy):
    client, _ = proxy
    await _start(client)
    resp = await client.get(f"{BASE}/index.m3u8")
    assert resp.status_code == 200
    names = [line for line in resp.text.splitlines() if line.endswith(".ts")]
    assert names == ["00000.ts", "00003.ts", "00004.ts", "00005.ts"]
    assert "#EXT-X-DISCONTINUITY" in resp.text
    assert resp.headers["content-type"].startswith("application/vnd.apple.mpegurl")


async def test_a_range_request_for_the_playlist_still_gets_the_whole_edited_playlist(proxy, plex):
    client, _ = proxy
    await _start(client)
    resp = await client.get(f"{BASE}/index.m3u8", headers={"range": "bytes=0-1"})
    assert resp.status_code == 200
    assert "00001.ts" not in resp.text and "00003.ts" in resp.text
    assert "range" not in plex.seen[-1].headers


async def test_playlist_for_an_unknown_session_passes_through_unedited(proxy):
    client, _ = proxy
    resp = await client.get(f"{BASE}/index.m3u8")
    assert resp.text == VARIANT


async def test_an_upstream_error_on_the_playlist_is_passed_on(proxy, plex):
    client, _ = proxy
    await _start(client)
    plex.variant_status = 404
    resp = await client.get(f"{BASE}/index.m3u8")
    assert resp.status_code == 404


async def test_segment_requests_are_forwarded_and_recorded(proxy):
    client, _ = proxy
    await _start(client)
    await client.get(f"{BASE}/index.m3u8")
    resp = await client.get(f"{BASE}/00003.ts")
    assert resp.status_code == 200 and resp.content == b"\x47" * 188
    report = (await client.get("/_proxy/report")).json()
    assert report["requests"] == [{"session": SESSION, "segment": 3}]
    assert report["sessions"][SESSION]["spans"] == [[5000, 15000]]


async def test_start_offset_shifts_where_the_cut_lands(proxy):
    client, _ = proxy
    await _start(client, offset="60")  # stream starts at 60 s, so the 7-12 s range is behind it
    resp = await client.get(f"{BASE}/index.m3u8")
    assert resp.text == VARIANT


async def test_other_requests_pass_through(proxy, plex):
    client, _ = proxy
    resp = await client.get("/library/sections", params={"a": "b"})
    assert resp.json() == {"ok": True}
    assert plex.seen[-1].url.params["a"] == "b"


async def test_telemetry_is_recorded_and_reported(proxy):
    client, _ = proxy
    await client.post("/_proxy/telemetry", json={"type": "tick", "t": 5.0})
    assert (await client.get("/_proxy/report")).json()["telemetry"] == [{"type": "tick", "t": 5.0}]


async def test_test_page_embeds_the_title(proxy):
    client, _ = proxy
    resp = await client.get("/_proxy/test", params={"rating_key": "73438"})
    assert resp.status_code == 200 and "/library/metadata/73438" in resp.text


async def test_a_clients_accept_encoding_is_forwarded(proxy, plex):
    client, _ = proxy
    await client.get("/library/sections", headers={"accept-encoding": "gzip"})
    assert plex.seen[-1].headers["accept-encoding"] == "gzip"


# ── the three conditions (client, user filter, eligible segments) ─────────────────

def _policy(*, controllable=False, user="alice", enabled=True, ranges=((7_000, 12_000),), unknown_filtered=True):
    async def client_controllable(client_id, remote_ip):
        return controllable

    async def resolve_user(token):
        return user

    async def filter_enabled(username):
        return enabled

    async def ranges_for(rating_key, username):
        return list(ranges)

    return Policy(ranges_for=ranges_for, client_controllable=client_controllable, resolve_user=resolve_user,
                  filter_enabled=filter_enabled, unknown_user_filtered=unknown_filtered)


async def _proxy(plex, policy):
    app = create_proxy_app("http://plex.test", policy, token="T", transport=httpx.MockTransport(plex))
    ctx = app.router.lifespan_context(app)
    await ctx.__aenter__()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy")
    return client, ctx


@pytest.mark.parametrize("policy, expected", [
    (_policy(), True),
    (_policy(controllable=True), False),
    (_policy(enabled=False), False),
    (_policy(ranges=()), False),
])
async def test_the_stream_is_forced_and_edited_only_when_all_three_conditions_hold(plex, policy, expected):
    client, ctx = await _proxy(plex, policy)
    try:
        await client.get("/video/:/transcode/universal/decision", params={
            "path": "/library/metadata/42", "session": SESSION, "directPlay": "1", "directStream": "1"})
        decision_params = plex.seen[-1].url.params
        await _start(client)
        start_params = plex.seen[-1].url.params
        resp = await client.get(f"{BASE}/index.m3u8")
    finally:
        await client.aclose()
        await ctx.__aexit__(None, None, None)
    forced = decision_params["directPlay"] == "0" and start_params["directStream"] == "0"
    assert forced is expected
    assert (resp.text != VARIANT) is expected


async def test_a_client_left_alone_keeps_the_direct_play_it_asked_for(plex):
    client, ctx = await _proxy(plex, _policy(controllable=True))
    try:
        await _start(client)
    finally:
        await client.aclose()
        await ctx.__aexit__(None, None, None)
    assert plex.seen[-1].url.params["directPlay"] == "1"


async def test_the_decision_is_made_once_per_session(plex):
    calls = []

    async def ranges_for(rating_key, username):
        calls.append(rating_key)
        return [(7_000, 12_000)]

    client, ctx = await _proxy(plex, Policy(ranges_for=ranges_for))
    try:
        for path in ("decision", "start.m3u8"):
            await client.get(f"/video/:/transcode/universal/{path}",
                             params={"path": "/library/metadata/42", "session": SESSION})
    finally:
        await client.aclose()
        await ctx.__aexit__(None, None, None)
    assert calls == ["42"]


async def test_report_lists_each_decision_with_its_reason(plex):
    client, ctx = await _proxy(plex, _policy(enabled=False, user="bob"))
    try:
        await _start(client)
        report = (await client.get("/_proxy/report")).json()
    finally:
        await client.aclose()
        await ctx.__aexit__(None, None, None)
    assert report["decisions"][SESSION] == {"inject": False, "reason": "filtering is off for bob", "username": "bob"}


# ── progress reports are translated back to the film's time ──────────────────────

async def _playing(client):
    """Start the session and fetch its playlist, so its cut spans (5-15 s) are known."""
    await _start(client, **{"X-Plex-Client-Identifier": "tv-1"})
    await client.get(f"{BASE}/index.m3u8")


@pytest.mark.parametrize("path", ["/:/timeline", "/:/progress"])
async def test_a_progress_report_after_a_cut_is_shifted_by_the_removed_time(proxy, plex, path):
    client, _ = proxy
    await _playing(client)

    await client.get(path, params={"time": "20000", "ratingKey": "42", "X-Plex-Client-Identifier": "tv-1"})

    assert plex.seen[-1].url.params["time"] == "30000"


async def test_a_position_before_the_first_cut_is_unchanged(proxy, plex):
    client, _ = proxy
    await _playing(client)
    await client.get("/:/timeline", params={"time": "3000", "ratingKey": "42", "X-Plex-Client-Identifier": "tv-1"})
    assert plex.seen[-1].url.params["time"] == "3000"


async def test_the_session_is_found_by_its_session_identifier_header(proxy, plex):
    client, _ = proxy
    await _playing(client)
    await client.get("/:/timeline", params={"time": "20000"}, headers={"x-plex-session-identifier": SESSION})
    assert plex.seen[-1].url.params["time"] == "30000"


async def test_the_title_can_be_named_by_its_key(proxy, plex):
    client, _ = proxy
    await _playing(client)
    await client.get("/:/timeline", params={"time": "20000", "key": "/library/metadata/42",
                                            "X-Plex-Client-Identifier": "tv-1"})
    assert plex.seen[-1].url.params["time"] == "30000"


async def test_a_report_for_a_session_the_proxy_did_not_edit_passes_through(proxy, plex):
    client, _ = proxy
    await client.get("/:/timeline", params={"time": "20000", "ratingKey": "42", "X-Plex-Client-Identifier": "other"})
    assert plex.seen[-1].url.params["time"] == "20000"


async def test_a_non_numeric_time_and_other_requests_are_left_alone(proxy, plex):
    client, _ = proxy
    await _playing(client)
    await client.get("/:/timeline", params={"time": "soon", "ratingKey": "42", "X-Plex-Client-Identifier": "tv-1"})
    assert plex.seen[-1].url.params["time"] == "soon"
    await client.get("/library/metadata/42", params={"time": "20000", "X-Plex-Client-Identifier": "tv-1"})
    assert plex.seen[-1].url.params["time"] == "20000"


async def test_a_client_left_alone_is_not_remapped(plex):
    client, ctx = await _proxy(plex, _policy(controllable=True))
    try:
        await _start(client, **{"X-Plex-Client-Identifier": "tv-1"})
        await client.get("/:/timeline", params={"time": "20000", "ratingKey": "42", "X-Plex-Client-Identifier": "tv-1"})
    finally:
        await client.aclose()
        await ctx.__aexit__(None, None, None)
    assert plex.seen[-1].url.params["time"] == "20000"


# ── WebSocket passthrough ───────────────────────────────────────────────────────

class FakeUpstreamSocket:
    """Stands in for Plex's notification socket."""

    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []

    def __aiter__(self):
        return self._messages()

    async def _messages(self):
        for message in self.incoming:
            yield message

    async def send(self, message):
        self.sent.append(message)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_the_notification_socket_is_passed_through_both_ways(plex):
    from starlette.testclient import TestClient

    upstream = FakeUpstreamSocket(['{"NotificationContainer": 1}', b"\x01\x02"])
    urls = []

    def connect(url):
        urls.append(url)
        return upstream

    app = create_proxy_app("http://plex.test:32400", Policy(ranges_for=static_ranges([])), transport=httpx.MockTransport(plex),
                           ws_connect=connect)
    with TestClient(app) as client, client.websocket_connect("/:/websockets/notifications?X-Plex-Token=abc") as ws:
        assert ws.receive_text() == '{"NotificationContainer": 1}'
        assert ws.receive_bytes() == b"\x01\x02"
        ws.send_text("hello")

    assert urls == ["ws://plex.test:32400/:/websockets/notifications?X-Plex-Token=abc"]
    assert upstream.sent in (["hello"], [])  # the client may close before the relay task runs


def test_an_unreachable_upstream_socket_closes_the_client_cleanly(plex):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    def connect(url):
        raise OSError("refused")

    app = create_proxy_app("http://plex.test", Policy(ranges_for=static_ranges([])), transport=httpx.MockTransport(plex),
                           ws_connect=connect)
    with TestClient(app) as client, client.websocket_connect("/:/websockets/notifications") as ws:
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
