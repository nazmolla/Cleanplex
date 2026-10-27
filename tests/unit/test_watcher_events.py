"""The session watcher announces stream changes (issue #89)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cleanplex import events, watcher


class _Stop(Exception):
    pass


async def _poll(monkeypatch, polls):
    """Run the watcher for len(polls) iterations; return the events it published."""
    sessions = iter(polls)
    client = MagicMock()
    client.get_active_sessions = AsyncMock(side_effect=lambda: next(sessions))
    config = SimpleNamespace(is_configured=lambda: True, pre_buffer_ms=0, post_buffer_ms=0, poll_interval=1)
    calls = {"n": 0}

    async def fake_sleep(_):
        calls["n"] += 1
        if calls["n"] >= len(polls):
            raise _Stop

    monkeypatch.setattr(watcher, "_last_sessions_signature", ())
    with events.subscribe() as q, \
         patch.object(watcher.asyncio, "sleep", fake_sleep), \
         patch.object(watcher, "_next_poll_delay", AsyncMock(return_value=0)), \
         patch.object(watcher.filter_engine, "reap", AsyncMock()), \
         patch.object(watcher.db, "get_user_filter", AsyncMock(return_value={"enabled": False})):
        with pytest.raises(_Stop):
            await watcher.session_watcher_loop(AsyncMock(return_value=config), lambda: client)
        return [t for t, _ in (q.get_nowait() for _ in range(q.qsize()))]


def _s(key, pos):
    return SimpleNamespace(session_key=key, position_ms=pos, user="u")


async def test_stream_start_move_and_stop_are_announced_once_each(monkeypatch):
    published = await _poll(monkeypatch, [
        [_s("a", 1000)],
        [_s("a", 1400)],   # same whole second: no event
        [_s("a", 6000)],   # moved
        [],                # stopped
        [],                # still nothing: no event
    ])

    assert published == ["sessions_changed"] * 3
