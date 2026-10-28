"""Hover previews (#114): evening job, DB state and the ffmpeg render, against real SQLite."""

from __future__ import annotations

import shutil
import subprocess
from datetime import time

import pytest

from cleanplex import database as db
from cleanplex import frame_extractor, hover_previews
from cleanplex.config import Config

pytestmark = pytest.mark.usefixtures("setup_db")


@pytest.fixture(autouse=True)
def hover_dir(tmp_path, monkeypatch):
    path = tmp_path / "hover"
    monkeypatch.setattr(hover_previews, "HOVER_DIR", path)
    return path


async def _job(file_path, guid="g"):
    await db.upsert_scan_job(plex_guid=guid, title="T", file_path=str(file_path), rating_key="1",
                             library_id="lib", library_title="Lib")


def _fake_render(calls, ok=True):
    async def render(file_path, start_ms, duration_ms, out_path):
        calls.append((start_ms, duration_ms))
        if ok:
            out_path.write_bytes(b"mp4")
        return ok
    return render


async def _always():
    return True


async def test_renders_pending_active_segments_and_skips_false_positives(tmp_path, monkeypatch, hover_dir):
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"x")
    await _job(src)
    keep = await db.insert_segment("g", "T", 1000, 9000)
    wrong = await db.insert_segment("g", "T", 20000, 25000)
    await db.mark_segment_false_positive(wrong)
    calls = []
    monkeypatch.setattr(frame_extractor, "render_hover_clip", _fake_render(calls))

    assert await hover_previews.render_pending(_always) == 1

    assert calls == [(1000, 8000)]
    assert (hover_dir / f"{keep}.mp4").is_file()
    assert (await db.get_segment_by_id(keep))["hover_status"] == "done"
    assert (await db.get_segment_by_id(wrong))["hover_status"] == ""
    # A second run finds nothing left to do.
    assert await hover_previews.render_pending(_always) == 0


async def test_nothing_is_rendered_once_the_window_closes(tmp_path, monkeypatch):
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"x")
    await _job(src)
    await db.insert_segment("g", "T", 0, 1000)
    calls = []
    monkeypatch.setattr(frame_extractor, "render_hover_clip", _fake_render(calls))

    async def closed():
        return False

    assert await hover_previews.render_pending(closed) == 0
    assert calls == []


async def test_failed_renders_are_not_retried(tmp_path, monkeypatch):
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"x")
    await _job(src)
    seg = await db.insert_segment("g", "T", 0, 1000)
    calls = []
    monkeypatch.setattr(frame_extractor, "render_hover_clip", _fake_render(calls, ok=False))

    await hover_previews.render_pending(_always)
    await hover_previews.render_pending(_always)

    assert len(calls) == 1
    assert (await db.get_segment_by_id(seg))["hover_status"] == "failed"


async def test_unreachable_source_is_retried_on_the_next_run(tmp_path, monkeypatch):
    src = tmp_path / "offline.mkv"
    await _job(src)
    seg = await db.insert_segment("g", "T", 0, 1000)
    calls = []
    monkeypatch.setattr(frame_extractor, "render_hover_clip", _fake_render(calls))

    assert await hover_previews.render_pending(_always) == 0
    assert (await db.get_segment_by_id(seg))["hover_status"] == "missing"

    src.write_bytes(b"x")  # the share is back
    assert await hover_previews.render_pending(_always) == 1
    assert (await db.get_segment_by_id(seg))["hover_status"] == "done"


async def test_files_of_deleted_segments_are_removed(tmp_path, monkeypatch, hover_dir):
    await _job(tmp_path / "movie.mkv")
    seg = await db.insert_segment("g", "T", 0, 1000)
    hover_dir.mkdir()
    (hover_dir / f"{seg}.mp4").write_bytes(b"keep")
    (hover_dir / "999999.mp4").write_bytes(b"orphan")
    (hover_dir / f"{seg}.mp4.part").write_bytes(b"half")

    await hover_previews.render_pending(_always)

    assert sorted(p.name for p in hover_dir.iterdir()) == [f"{seg}.mp4"]


async def test_db_helpers_handle_empty_tables():
    assert await db.get_hover_preview_candidates(10) == []
    assert await db.get_segment_ids() == set()
    assert await db.reset_missing_hover_previews() == 0


def test_hover_window_defaults_to_overnight():
    config = Config()
    assert (config.hover_window_start, config.hover_window_end) == (time(0, 0), time(7, 0))
    assert Config(hover_window_start=time(0, 0), hover_window_end=time(23, 59)).is_hover_window()


def test_long_segments_are_cut_and_sped_up_to_the_loop_length():
    cmd = frame_extractor._hover_cmd("/m.mkv", 10_000, 300_000, "/o.mp4", hardware=False)
    assert cmd[cmd.index("-t") + 1] == "60.000"
    assert "setpts=PTS/12.000" in cmd[cmd.index("-vf") + 1]
    assert "-an" in cmd and "-hwaccel" not in cmd
    short = frame_extractor._hover_cmd("/m.mkv", 0, 3000, "/o.mp4", hardware=True)
    assert "setpts=PTS/1.000" in short[short.index("-vf") + 1]
    assert short[short.index("-hwaccel") + 1] == "auto"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_render_hover_clip_writes_a_small_silent_mp4(tmp_path):
    src = tmp_path / "src.mkv"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=24",
         "-f", "lavfi", "-i", "sine", "-t", "12", "-c:v", "mpeg4", "-c:a", "mp2", str(src)],
        check=True,
    )
    out = tmp_path / "1.mp4"

    assert await frame_extractor.render_hover_clip(str(src), 1000, 10_000, out) is True

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width:format=duration",
         "-of", "default=nw=1", str(out)],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "codec_type=video" in probe and "codec_type=audio" not in probe
    assert "width=320" in probe
    assert float(probe.split("duration=")[1].split()[0]) <= frame_extractor.HOVER_LOOP_S + 0.5
    assert not (tmp_path / "1.mp4.part").exists()


async def _run_loop_once(monkeypatch, config, render):
    import asyncio

    async def stop(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(hover_previews, "render_pending", render)
    monkeypatch.setattr(hover_previews.asyncio, "sleep", stop)

    async def get_config():
        return config

    with pytest.raises(asyncio.CancelledError):
        await hover_previews.hover_preview_loop(get_config)


async def test_loop_renders_only_inside_the_window_when_enabled(monkeypatch):
    runs = []

    async def render(should_continue):
        runs.append(await should_continue())
        return 0

    all_day = dict(hover_window_start=time(0, 0), hover_window_end=time(23, 59))
    await _run_loop_once(monkeypatch, Config(**all_day), render)
    await _run_loop_once(monkeypatch, Config(hover_previews_enabled=False, **all_day), render)
    await _run_loop_once(monkeypatch, Config(hover_window_start=time(0, 0), hover_window_end=time(0, 0)), render)

    assert runs == [True]


async def test_loop_survives_a_failed_run(monkeypatch):
    async def boom(should_continue):
        raise RuntimeError("disk full")

    config = Config(hover_window_start=time(0, 0), hover_window_end=time(23, 59))
    # The error is logged and the loop carries on to its sleep (which stops the test).
    await _run_loop_once(monkeypatch, config, boom)
