"""Unit tests for frame_extractor preview clips (#80, #93) and scan frame sizing (#82), using real ffmpeg."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from cleanplex import frame_extractor

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


async def test_render_clip_turns_mkv_into_a_seekable_mp4(tmp_path):
    src = tmp_path / "src.mkv"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
         "-f", "lavfi", "-i", "sine", "-t", "4", "-c:v", "mpeg4", "-c:a", "mp2", str(src)],
        check=True,
    )
    out = tmp_path / "clip.mp4"

    assert await frame_extractor.render_clip(str(src), 1000, 2000, out) is True

    data = out.read_bytes()
    assert data[4:8] == b"ftyp"
    # +faststart: the index (moov) comes before the media data (mdat).
    assert data.index(b"moov") < data.index(b"mdat")
    assert not (tmp_path / "clip.mp4.part").exists()


async def test_render_clip_reports_failure_and_leaves_no_file(tmp_path):
    out = tmp_path / "clip.mp4"

    assert await frame_extractor.render_clip(str(tmp_path / "nope.mkv"), 0, 1000, out) is False
    assert not out.exists() and not (tmp_path / "clip.mp4.part").exists()


# ── Scan frame sizing (issue #82) ─────────────────────────────────────────────

def _make_video(path, size):
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=5",
         "-t", "2", "-c:v", "mpeg4", str(path)],
        check=True,
    )


async def _first_frame_size(path, size):
    import cv2
    import numpy as np

    async for _, jpeg in frame_extractor.extract_frames_batch(str(path), 1000, 2000, size):
        mat = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        return mat.shape[1], mat.shape[0]


@pytest.mark.parametrize("size, expected", [(320, (320, 180)), (640, (640, 360))])
async def test_landscape_frames_match_the_model_input_width(tmp_path, size, expected):
    src = tmp_path / "wide.mkv"
    _make_video(src, "1920x1080")

    assert await _first_frame_size(src, size) == expected


async def test_portrait_frames_are_bounded_by_height(tmp_path):
    src = tmp_path / "tall.mkv"
    _make_video(src, "720x1280")

    assert await _first_frame_size(src, 640) == (360, 640)
