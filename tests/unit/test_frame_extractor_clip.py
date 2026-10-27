"""Unit tests for frame_extractor.stream_clip against a real ffmpeg (issue #80)."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from cleanplex import frame_extractor

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


async def test_stream_clip_turns_mkv_into_fragmented_mp4(tmp_path):
    src = tmp_path / "src.mkv"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
         "-f", "lavfi", "-i", "sine", "-t", "4", "-c:v", "mpeg4", "-c:a", "mp2", str(src)],
        check=True,
    )

    data = b"".join([c async for c in frame_extractor.stream_clip(str(src), 1000, 2000)])

    assert data[4:8] == b"ftyp"
    assert b"moof" in data


async def test_stream_clip_yields_nothing_for_an_unreadable_file(tmp_path):
    data = b"".join([c async for c in frame_extractor.stream_clip(str(tmp_path / "nope.mkv"), 0, 1000)])

    assert data == b""

