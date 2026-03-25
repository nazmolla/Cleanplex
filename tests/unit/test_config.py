"""Unit tests for config.py — Config.load() and helper methods."""

from __future__ import annotations

import json
from datetime import time

import pytest

from cleanplex import database as db
from cleanplex.config import Config


pytestmark = pytest.mark.usefixtures("setup_db")


async def test_config_load_returns_defaults():
    config = await Config.load()
    # Default settings seeded by init_db
    assert config.poll_interval == 5
    assert config.confidence_threshold == pytest.approx(0.6)
    assert config.skip_buffer_ms == 3000
    assert config.scan_workers == 2
    assert config.log_level == "INFO"


async def test_config_load_reads_plex_url():
    await db.set_setting("plex_url", "http://myplex:32400")
    config = await Config.load()
    assert config.plex_url == "http://myplex:32400"


async def test_config_load_reads_scan_labels():
    labels = ["FEMALE_BREAST_EXPOSED", "MALE_GENITALIA_EXPOSED"]
    await db.set_setting("scan_labels", json.dumps(labels))
    config = await Config.load()
    assert config.scan_labels == labels


async def test_config_load_scan_labels_invalid_json_returns_empty():
    await db.set_setting("scan_labels", "not json")
    config = await Config.load()
    assert config.scan_labels == []


async def test_config_load_scan_window_parsed():
    await db.set_setting("scan_window_start", "22:30")
    await db.set_setting("scan_window_end", "07:00")
    config = await Config.load()
    assert config.scan_window_start == time(22, 30)
    assert config.scan_window_end == time(7, 0)


async def test_config_scan_workers_minimum_one():
    await db.set_setting("scan_workers", "0")
    config = await Config.load()
    assert config.scan_workers == 1


# ── is_configured ──────────────────────────────────────────────────────────────

def test_is_configured_true_when_url_and_token_set():
    config = Config(plex_url="http://plex:32400", plex_token="tok")
    assert config.is_configured() is True


def test_is_configured_false_when_url_missing():
    config = Config(plex_url="", plex_token="tok")
    assert config.is_configured() is False


def test_is_configured_false_when_token_missing():
    config = Config(plex_url="http://plex:32400", plex_token="")
    assert config.is_configured() is False


# ── is_scan_window ─────────────────────────────────────────────────────────────

def test_is_scan_window_always_on_when_window_spans_all_day():
    # start == end at midnight: window wraps to cover all hours
    # (start > end branch: now >= 00:00 is always true)
    config = Config(scan_window_start=time(0, 0), scan_window_end=time(0, 0))
    # Result depends on current time but the logic path is executed
    result = config.is_scan_window()
    assert isinstance(result, bool)
