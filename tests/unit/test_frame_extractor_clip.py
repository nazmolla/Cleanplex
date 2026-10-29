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

    frames = frame_extractor.extract_frames_batch(str(path), 1000, 2000, size)
    try:
        async for _, jpeg in frames:
            mat = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            return mat.shape[1], mat.shape[0]
    finally:
        await frames.aclose()  # stop ffmpeg now, not when the generator is collected


@pytest.mark.parametrize("size, expected", [(320, (320, 180)), (640, (640, 360))])
async def test_landscape_frames_match_the_model_input_width(tmp_path, size, expected):
    src = tmp_path / "wide.mkv"
    _make_video(src, "1920x1080")

    assert await _first_frame_size(src, size) == expected


async def test_portrait_frames_are_bounded_by_height(tmp_path):
    src = tmp_path / "tall.mkv"
    _make_video(src, "720x1280")

    assert await _first_frame_size(src, 640) == (360, 640)


# ── Hardware decoding and stalls (issues #107, #108) ─────────────────────────

async def _collect(gen):
    return [x async for x in gen]


async def test_hardware_flag_asks_ffmpeg_for_any_available_decoder(tmp_path, monkeypatch):
    src = tmp_path / "v.mkv"
    _make_video(src, "320x240")
    seen = []
    real = frame_extractor.asyncio.create_subprocess_exec

    async def spy(*cmd, **kw):
        seen.append(cmd)
        return await real(*cmd, **kw)

    monkeypatch.setattr(frame_extractor.asyncio, "create_subprocess_exec", spy)

    frames = await _collect(frame_extractor.extract_frames_batch(str(src), 1000, 2000, 320, hardware=True))
    await _collect(frame_extractor.extract_frames_batch(str(src), 1000, 2000, 320, hardware=False))

    assert frames, "hwaccel auto must still produce frames (software where no hardware exists)"
    assert ("-hwaccel", "auto") == seen[0][seen[0].index("-hwaccel"):seen[0].index("-hwaccel") + 2]
    assert "-hwaccel" not in seen[-1]


async def test_a_file_the_hardware_decoder_cannot_read_is_retried_in_software(monkeypatch):
    calls = []

    async def fake_stream(path, step, dur, size, hardware):
        calls.append(hardware)
        if not hardware:
            yield 0, b"frame"

    monkeypatch.setattr(frame_extractor, "_frame_stream", fake_stream)

    frames = await _collect(frame_extractor.extract_frames_batch("x.mkv", 1000, 1000, 320, hardware=True))

    assert calls == [True, False]
    assert frames == [(0, b"frame")]


class _SilentProc:
    returncode = None

    class stdout:
        @staticmethod
        async def read(n):
            await frame_extractor.asyncio.sleep(10)

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


async def test_a_silent_ffmpeg_fails_the_stream_instead_of_ending_it(monkeypatch):
    """Regression for #108: a stall used to end the stream, so the scan was saved as done."""
    monkeypatch.setattr(frame_extractor, "FRAME_STALL_TIMEOUT_S", 0.05)

    async def fake_exec(*cmd, **kw):
        return _SilentProc()

    monkeypatch.setattr(frame_extractor.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(frame_extractor.FrameStreamStalled):
        await _collect(frame_extractor.extract_frames_batch("x.mkv", 1000, 60_000, 320, hardware=False))


# ── Dense window sampling (#133) ──────────────────────────────────────────────

async def test_window_frames_sit_at_absolute_offsets_and_show_the_right_instants(tmp_path):
    """Brightness rises 25 per second, so each frame's brightness says what time it really is."""
    import cv2
    import numpy as np

    src = tmp_path / "ramp.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=black:s=64x64:r=10,geq=lum='min(255,T*25)':cb=128:cr=128",
         "-t", "10", "-c:v", "libx264", "-g", "10", str(src)],
        check=True,
    )

    frames = [(t, jpg) async for t, jpg in frame_extractor.extract_frames_window(str(src), 4000, 3000, 500, 64)]

    assert [t for t, _ in frames] == [4000, 4500, 5000, 5500, 6000, 6500]
    for t, jpg in frames:
        luma = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_GRAYSCALE).mean()
        assert luma == pytest.approx(t / 1000 * 25, abs=14)  # within about half a second


async def test_window_stops_at_the_end_of_the_window_not_the_file(tmp_path):
    src = tmp_path / "long.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10", "-t", "20",
         "-c:v", "libx264", str(src)],
        check=True,
    )
    frames = [t async for t, _ in frame_extractor.extract_frames_window(str(src), 2000, 2000, 500, 64)]
    assert frames == [2000, 2500, 3000, 3500]
