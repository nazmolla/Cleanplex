"""Keyframe index of flagged media files, so the stream proxy can cut copied (not re-encoded) streams.

In copy mode Plex starts each HLS segment on a keyframe of the original file near the
playlist's nominal time (#184, measured: the first keyframe at or after it, occasionally
one up to ~1.2 s before). Knowing a file's keyframes lets the proxy bound every segment's
real span and drop all that could touch a flagged range, without re-encoding the video.
"""

from __future__ import annotations

import asyncio
import os

from . import database as db
from .frame_extractor import _FFPROBE_BIN
from .logger import get_logger

logger = get_logger(__name__)

BATCH = 1             # files per round; each is a full read of the file (a 4K remux is tens of GB)
BETWEEN_S = 60        # pause between files so indexing never competes with playback or scans
IDLE_S = 900          # nothing to index: look again after this long
PROBE_TIMEOUT_S = 900  # a 4K remux over a network share can take minutes to read


def file_signature(path: str) -> tuple[int, int] | None:
    """(size, mtime) of a file, or None when it cannot be read."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, int(st.st_mtime)


async def probe_keyframes(path: str) -> list[int] | None:
    """Read a file's video keyframe times (ms, file time) with ffprobe; None on failure."""
    proc = await asyncio.create_subprocess_exec(
        _FFPROBE_BIN, "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
        "-show_entries", "frame=pts_time", "-of", "csv=p=0", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), PROBE_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    if proc.returncode != 0:
        return None
    times = []
    for line in out.decode(errors="ignore").splitlines():
        value = line.strip().rstrip(",")
        try:
            times.append(round(float(value) * 1000))
        except ValueError:
            continue
    return sorted(set(times)) or None


async def keyframes_for(path: str) -> list[int] | None:
    """Cached keyframes for *path* if the file is unchanged since it was indexed."""
    sig = file_signature(path)
    if not sig:
        return None
    return await db.get_keyframes(path, *sig)


async def index_once() -> int:
    """Index up to BATCH flagged files; return how many were stored."""
    stored = 0
    for path in await db.get_flagged_files_without_keyframes(BATCH):
        sig = file_signature(path)
        if not sig:
            continue
        keyframes = await probe_keyframes(path)
        # An empty or failed probe is stored as [] so the file is not read again on every
        # round; the proxy treats [] like a missing index and re-encodes instead.
        await db.put_keyframes(path, *sig, keyframes or [])
        stored += 1
    return stored


async def keyframe_index_loop() -> None:
    """Keep the index current for titles with active skip segments."""
    while True:
        delay = IDLE_S
        try:
            if await index_once():
                delay = BETWEEN_S
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Keyframe index error: %s", exc)
        await asyncio.sleep(delay)
