"""Regression tests for reaping ffmpeg when a frame stream is closed early (#218)."""

import asyncio
import stat
import sys
from contextlib import aclosing

from cleanplex import frame_extractor

# Stands in for ffmpeg: writes JPEG-delimited frames forever, ignoring its arguments.
FAKE_FFMPEG = """#!{python}
import sys
frame = b"\\xff\\xd8" + b"x" * 60000 + b"\\xff\\xd9"
out = sys.stdout.buffer
while True:
    out.write(frame)
"""


def _fake_ffmpeg(tmp_path):
    path = tmp_path / "ffmpeg"
    path.write_text(FAKE_FFMPEG.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


async def test_closing_frame_stream_after_pipe_fills_does_not_hang(tmp_path, monkeypatch):
    monkeypatch.setattr(frame_extractor, "_FFMPEG_BIN", _fake_ffmpeg(tmp_path))

    async def read_one_then_close():
        async with aclosing(frame_extractor._frame_stream("in.mkv", 5000, 10**9, 320, False)) as frames:
            async for _ in frames:
                # The consumer is busy (inference) while ffmpeg keeps writing: the pipe fills and pauses.
                await asyncio.sleep(0.5)
                break

    # Before the fix, wait() on the killed process blocked here forever.
    await asyncio.wait_for(read_one_then_close(), timeout=10)


async def test_reap_process_returns_for_a_process_with_unread_output(tmp_path):
    proc = await asyncio.create_subprocess_exec(
        _fake_ffmpeg(tmp_path), stdout=asyncio.subprocess.PIPE,
    )
    await proc.stdout.read(131072)
    await asyncio.sleep(0.5)
    await asyncio.wait_for(frame_extractor.reap_process(proc), timeout=5)
    assert proc.returncode is not None
