"""The stream proxy prototype in front of a fake Plex (#132)."""

from __future__ import annotations

import httpx
import pytest

from cleanplex.stream_proxy import create_proxy_app, static_ranges

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
    app = create_proxy_app("http://plex.test", static_ranges([(7_000, 12_000)]), token="TESTTOKEN",
                           transport=httpx.MockTransport(plex))
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
    await client.get("/video/:/transcode/universal/decision", params={"directPlay": "1", "directStream": "1"})
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


async def test_db_ranges_widen_each_skip_segment_by_the_buffers(setup_db):
    from cleanplex import database as db
    from cleanplex.stream_proxy import db_ranges

    await db.upsert_scan_job(plex_guid="g", title="T", file_path="/m.mkv", rating_key="42",
                             library_id="l", library_title="L")
    await db.insert_segment("g", "T", 10_000, 20_000)
    await db.insert_segment("g", "T", 1_000, 2_000)

    assert await db_ranges(3_000, 4_000)("42") == [(0, 6_000), (7_000, 24_000)]
