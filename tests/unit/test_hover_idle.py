"""Hover previews render as soon as the scan queue is idle (#152)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from cleanplex import hover_previews
from cleanplex.config import Config


async def _one_pass(config, idle):
    """Run the loop for a single iteration; return how many times render_pending was called."""
    sleeps = {"n": 0}

    async def get_config():
        return config

    async def fake_sleep(_):
        sleeps["n"] += 1
        raise asyncio.CancelledError

    render = AsyncMock(return_value=0)
    with patch.object(hover_previews, "render_pending", render), patch.object(hover_previews.asyncio, "sleep", fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await hover_previews.hover_preview_loop(get_config, scanner_idle=lambda: idle)
    return render.await_count


def _cfg(**over):
    # A window that is closed all day, so only the idle rule can open it.
    base = replace(Config(), hover_previews_enabled=True, hover_when_idle=True)
    return replace(base, **over)


async def test_it_renders_outside_the_window_when_the_scanner_is_idle():
    with patch.object(Config, "is_hover_window", return_value=False):
        assert await _one_pass(_cfg(), idle=True) == 1


async def test_it_does_not_render_while_a_scan_is_queued_or_running():
    with patch.object(Config, "is_hover_window", return_value=False):
        assert await _one_pass(_cfg(), idle=False) == 0


async def test_with_the_setting_off_only_the_window_counts():
    with patch.object(Config, "is_hover_window", return_value=False):
        assert await _one_pass(_cfg(hover_when_idle=False), idle=True) == 0


async def test_inside_the_window_it_renders_whatever_the_scanner_is_doing():
    with patch.object(Config, "is_hover_window", return_value=True):
        assert await _one_pass(_cfg(), idle=False) == 1


async def test_a_disabled_job_never_renders():
    with patch.object(Config, "is_hover_window", return_value=True):
        assert await _one_pass(_cfg(hover_previews_enabled=False), idle=True) == 0


def test_the_service_treats_an_empty_queue_with_nothing_scanning_as_idle():
    from cleanplex import main

    with patch("cleanplex.scanner.get_queue_size", return_value=0), patch("cleanplex.scanner.get_current_scans", return_value=[]):
        assert main._scanner_idle() is True
    with patch("cleanplex.scanner.get_queue_size", return_value=3), patch("cleanplex.scanner.get_current_scans", return_value=[]):
        assert main._scanner_idle() is False
    with patch("cleanplex.scanner.get_queue_size", return_value=0), patch("cleanplex.scanner.get_current_scans", return_value=["g"]):
        assert main._scanner_idle() is False
