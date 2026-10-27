"""Event bus and SSE stream (issue #89)."""

from __future__ import annotations

import asyncio
import json

import pytest

from cleanplex import events
from cleanplex.web.routes import events_routes


async def test_published_events_reach_every_subscriber():
    with events.subscribe() as a, events.subscribe() as b:
        events.publish("scan_started", guid="g1")

        assert a.get_nowait() == ("scan_started", {"guid": "g1"})
        assert b.get_nowait() == ("scan_started", {"guid": "g1"})


def test_publish_without_subscribers_is_a_no_op():
    events.publish("settings_changed")

    assert events.subscriber_count() == 0


async def test_leaving_the_block_unsubscribes():
    with events.subscribe():
        assert events.subscriber_count() == 1

    assert events.subscriber_count() == 0


async def test_a_full_backlog_collapses_into_one_resync(monkeypatch):
    monkeypatch.setattr(events, "_QUEUE_SIZE", 3)
    with events.subscribe() as q:
        for i in range(5):
            events.publish("scan_progress", guid="g", progress=i / 5)

        drained = [q.get_nowait() for _ in range(q.qsize())]

    assert drained[0] == ("resync", {})
    assert len(drained) <= 3


def test_sse_wire_format():
    assert events.format_sse("skip_recorded", {"guid": "g"}) == 'event: skip_recorded\ndata: {"guid": "g"}\n\n'


class _FakeRequest:
    def __init__(self):
        self.disconnected = False

    async def is_disconnected(self):
        return self.disconnected


async def test_stream_sends_events_and_heartbeats_then_stops_on_disconnect(monkeypatch):
    monkeypatch.setattr(events_routes, "HEARTBEAT_S", 0.05)
    request = _FakeRequest()
    resp = await events_routes.stream_events(request)
    body = resp.body_iterator

    assert resp.media_type == "text/event-stream"
    assert (await body.__anext__()).startswith("retry:")
    events.publish("segments_changed", guid="g1")
    chunk = await asyncio.wait_for(body.__anext__(), 1)
    assert chunk.startswith("event: segments_changed\n")
    assert json.loads(chunk.split("data: ")[1]) == {"guid": "g1"}
    assert await asyncio.wait_for(body.__anext__(), 1) == ": ping\n\n"

    request.disconnected = True
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(body.__anext__(), 1)
    assert events.subscriber_count() == 0
