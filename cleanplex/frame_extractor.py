"""Extract video frames using ffmpeg — single-frame and batch modes."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import AsyncGenerator

from .logger import get_logger

logger = get_logger(__name__)

_FFMPEG_BIN: str = "ffmpeg"

# Known locations where media servers bundle ffmpeg/ffprobe
_FFMPEG_SEARCH_PATHS = [
    r"D:\ffmpeg\ffmpeg-8.1-essentials_build\bin\ffmpeg.exe",
    r"C:\Program Files\Jellyfin\Server\ffmpeg.exe",
    r"C:\Bazarr\bin\Windows\amd64\ffmpeg\ffmpeg.exe",
]
_FFPROBE_SEARCH_PATHS = [
    r"D:\ffmpeg\ffmpeg-8.1-essentials_build\bin\ffprobe.exe",
    r"C:\Program Files\Jellyfin\Server\ffprobe.exe",
    r"C:\Bazarr\bin\Windows\amd64\ffmpeg\ffprobe.exe",
]


def _find_bin(name: str, search_paths: list[str]) -> str:
    found = shutil.which(name)
    if found:
        return found
    for p in search_paths:
        if Path(p).exists():
            return p
    return name  # fall back to bare name, will fail gracefully


_FFMPEG_BIN = _find_bin("ffmpeg", _FFMPEG_SEARCH_PATHS)
_FFPROBE_BIN = _find_bin("ffprobe", _FFPROBE_SEARCH_PATHS)


def check_ffmpeg() -> bool:
    return Path(_FFMPEG_BIN).exists() or shutil.which("ffmpeg") is not None


async def extract_frame(file_path: str, offset_ms: int) -> bytes | None:
    """Return JPEG bytes of one frame at *offset_ms*, or None on failure."""
    offset_s = offset_ms / 1000.0
    cmd = [
        _FFMPEG_BIN,  # resolved at startup
        "-loglevel", "error",
        "-ss", str(offset_s),
        "-i", file_path,
        "-frames:v", "1",
        "-vf", "scale=320:-1",   # resize for faster inference
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "pipe:1",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0 or not stdout:
            logger.debug("ffmpeg error at %dms: %s", offset_ms, stderr.decode(errors="replace")[:200])
            return None
        return stdout
    except asyncio.TimeoutError:
        logger.warning("ffmpeg timed out at offset %dms for %s", offset_ms, file_path)
        proc.kill()
        return None
    except Exception as exc:
        logger.warning("Frame extraction error: %s", exc)
        return None


async def extract_frames_batch(
    file_path: str,
    step_ms: int,
    duration_ms: int,
) -> AsyncGenerator[tuple[int, bytes | None], None]:
    """Yield (offset_ms, jpeg_bytes) for every step_ms interval via one ffmpeg process.

    Uses a single ffmpeg invocation with the fps filter rather than spawning a new
    process per frame. On Windows this eliminates ~200-500 ms of process-creation
    overhead per frame. While NudeNet runs in the thread pool between yields, the
    event loop continues draining ffmpeg's stdout pipe, so extraction and inference
    overlap naturally.

    Splits the MJPEG pipe stream on JPEG SOI (FF D8) / EOI (FF D9) markers.
    The generator's finally block kills the ffmpeg process if the caller breaks
    early (skip/pause/window exit).

    Yields None for offset_ms values where ffmpeg produced no frame (e.g. the video
    ended earlier than duration_ms indicated).
    """
    # Express step as a rational fps fraction to avoid float rounding (e.g. 1000/5000).
    fps_str = f"1000/{step_ms}"
    cmd = [
        _FFMPEG_BIN,
        "-loglevel", "error",
        "-i", file_path,
        "-vf", f"fps={fps_str},scale=320:-2",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    buffer = bytearray()
    offset_ms = 0
    # SOI / EOI byte sequences that delimit each JPEG in the MJPEG stream.
    SOI = b"\xff\xd8"
    EOI = b"\xff\xd9"

    try:
        while True:
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(131_072), timeout=30)
            except asyncio.TimeoutError:
                logger.warning(
                    "ffmpeg batch read timed out at %d ms for %s", offset_ms, file_path
                )
                break
            if not chunk:
                break
            buffer.extend(chunk)

            # Extract every complete JPEG frame that has arrived in the buffer.
            while True:
                soi = buffer.find(SOI)
                if soi == -1:
                    buffer.clear()
                    break
                eoi = buffer.find(EOI, soi + 2)
                if eoi == -1:
                    # Incomplete frame — keep from SOI, wait for more data.
                    if soi > 0:
                        del buffer[:soi]
                    break
                frame = bytes(buffer[soi : eoi + 2])
                del buffer[: eoi + 2]
                if offset_ms <= duration_ms:
                    yield offset_ms, frame
                    offset_ms += step_ms
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


async def get_duration_ms(file_path: str) -> int | None:
    """Return video duration in milliseconds using ffprobe."""
    ffprobe = _FFPROBE_BIN
    if not Path(ffprobe).exists() and not shutil.which(ffprobe):
        logger.error("ffprobe not found. Checked: %s", ffprobe)
        return None
    cmd = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        duration_s = float(stdout.strip())
        return int(duration_s * 1000)
    except Exception:
        return None
