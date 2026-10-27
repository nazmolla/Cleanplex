"""Server-Sent Events stream of backend changes (see cleanplex/events.py)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ... import events

router = APIRouter(prefix="/api/events", tags=["events"])

# Comment line sent when idle so proxies keep the connection open and a closed tab
# is noticed (the write fails) within this many seconds.
HEARTBEAT_S = 15.0


@router.get("")
async def stream_events(request: Request):
    """Stream every backend change as an SSE event until the client disconnects."""

    async def body():
        with events.subscribe() as queue:
            # retry: how long EventSource waits before reconnecting after a drop.
            yield "retry: 3000\n\n"
            while True:
                try:
                    event_type, data = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield ": ping\n\n"
                    continue
                yield events.format_sse(event_type, data)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        # X-Accel-Buffering: stop reverse proxies (nginx) from buffering the stream.
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
