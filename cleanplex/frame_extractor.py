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


def check_ffmpeg() -> bool:
    return shutil.which(_FFMPEG_BIN) is not None


async def extract_frame(file_path: str, offset_ms: int) -> bytes | None:
    """Return JPEG bytes of one frame at *offset_ms*, or None on failure."""
    offset_s = offset_ms / 1000.0
    cmd = [
        _FFMPEG_BIN,
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
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
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
