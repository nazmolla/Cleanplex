"""In-process event bus: backend changes pushed to browsers over Server-Sent Events.

Publishers call publish() at the point a change happens; GET /api/events streams
every event to each connected browser, which refetches only the affected section.
Events say *what* changed, not the new state, so a missed or merged event can
never leave a page wrong for long: the page simply refetches.

Event types (data fields):
  scan_started    {guid}            a worker began scanning a title
  scan_progress   {guid, progress}  throttled while a scan runs (progress 0..1)
  scan_finished   {guid}            a scan ended (done, failed, paused or skipped)
  scanner_changed {}                queue, pause/resume or worker pool changed
  segments_changed {guid}           segments written or deleted ("" = many titles)
  titles_changed  {library_id}      titles added/removed/updated ("" = any library)
  skip_recorded   {guid}            a skip or mute fired (or failed) during playback
  sessions_changed {}               active streams or their positions changed
  settings_changed {}               settings saved
  users_changed   {username}        a user's filter toggle or category levels changed
  markers_changed {}                Plex markers re-synced
  resync          {}                this subscriber fell behind; refetch everything
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from typing import Iterator

# Per-subscriber backlog. A stalled tab must never block publishers or grow memory,
# so past this the backlog is replaced by a single resync event.
_QUEUE_SIZE = 256

# Only touched from the event loop thread (publishers are coroutines or code they
# call directly), so a plain set needs no lock.
_subscribers: set[asyncio.Queue[tuple[str, dict]]] = set()


def publish(event_type: str, **data) -> None:
    """Send an event to every subscriber without waiting. Call from the event loop."""
    for queue in list(_subscribers):
        try:
            queue.put_nowait((event_type, data))
        except asyncio.QueueFull:
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(("resync", {}))


@contextmanager
def subscribe() -> Iterator[asyncio.Queue[tuple[str, dict]]]:
    """Register a subscriber for the duration of the block."""
    queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue(_QUEUE_SIZE)
    _subscribers.add(queue)
    try:
        yield queue
    finally:
        _subscribers.discard(queue)


def subscriber_count() -> int:
    return len(_subscribers)


def format_sse(event_type: str, data: dict) -> str:
    """Encode one event in the text/event-stream wire format."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
