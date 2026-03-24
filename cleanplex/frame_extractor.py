"""Extract a single video frame at a given offset using ffmpeg."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

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
