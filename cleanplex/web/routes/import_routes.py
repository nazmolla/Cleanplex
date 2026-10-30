"""Import endpoints for externally produced skip files."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from ...logger import get_logger
from ... import database as db
from ... import events
from ... import importers
from ... import plex_summary
from ... import segment_files
from ...importers._common import ParseError

logger = get_logger(__name__)
router = APIRouter(prefix="/api/segments", tags=["import"])

# An imported file's timings only hold for the cut they were authored against.
# Beyond this difference between the file's implied runtime and the media's own,
# the import is still accepted but flagged, since the user may have a different
# release (theatrical vs extended, PAL speed-up, different edit).
DURATION_TOLERANCE_MS = 5000

# Uploaded skip files are small text documents; anything larger is not one.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024


class PastePayload(BaseModel):
    plex_guid: str
    text: str
    replace: bool = False


def _runtime_warning(segments: list[dict], duration_ms: int) -> str | None:
    """Return a warning when the file's span does not match the media's runtime."""
    if not duration_ms or not segments:
        return None
    implied = max(s["end_ms"] for s in segments)
    if implied > duration_ms + DURATION_TOLERANCE_MS:
        return (
            f"The file's last segment ends at {implied}ms but this title is only "
            f"{duration_ms}ms long. The timings were probably authored against a "
            "different release, so skips may land in the wrong place."
        )
    return None


async def _store(plex_guid: str, segments: list[dict], source: str, replace: bool) -> dict:
    """Persist parsed segments against a known scan job."""
    job = await db.get_scan_job_by_guid(plex_guid)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown title: {plex_guid}")

    if replace:
        await segment_files.delete_segments_for_guid(plex_guid)

    count = await db.insert_segments_bulk(plex_guid, job.get("title") or "", segments, source)
    events.publish("segments_changed", guid=plex_guid)
    plex_summary.refresh_soon(plex_guid)
    logger.info("Imported %d segment(s) for '%s' from %s", count, job.get("title"), source)

    return {
        "imported": count,
        "source": source,
        "title": job.get("title") or "",
        "warning": _runtime_warning(segments, int(job.get("duration_ms") or 0)),
    }


@router.post("/import")
async def import_skip_file(
    plex_guid: str = Form(...),
    file: UploadFile = File(...),
    replace: bool = Form(False),
):
    """Import a .skp, .edl, .mcf or .txt skip file for one title."""
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=422, detail="Skip file is too large to be one.")

    suffix = Path(file.filename or "").suffix.lower()
    module = importers.PARSERS.get(suffix)
    if module is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported skip file type: {suffix or file.filename!r}. "
                   "Expected .skp, .edl, .mcf or .txt.",
        )

    try:
        segments = module.parse(raw.decode("utf-8", errors="replace"))
    except ParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return await _store(plex_guid, segments, module.SOURCE, replace)


@router.post("/import/paste")
async def import_pasted_list(payload: PastePayload):
    """Import a loosely formatted timestamp list pasted by the user."""
    try:
        segments = importers.parse(payload.text, "paste")
    except ParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return await _store(payload.plex_guid, segments, "paste", payload.replace)


class SubtitleScanPayload(BaseModel):
    plex_guid: str
    replace: bool = False


@router.post("/scan-subtitles")
async def scan_subtitles(payload: SubtitleScanPayload):
    """Scan a title's subtitles for profanity and store mute segments."""
    from ... import subtitle_scanner

    job = await db.get_scan_job_by_guid(payload.plex_guid)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown title: {payload.plex_guid}")
    if not job.get("file_path"):
        raise HTTPException(status_code=422, detail="This title has no file path on disk.")

    if payload.replace:
        await segment_files.delete_segments_for_guid(payload.plex_guid)

    count = await subtitle_scanner.scan_title(
        payload.plex_guid, job.get("title") or "", job["file_path"]
    )
    plex_summary.refresh_soon(payload.plex_guid)
    return {"imported": count, "source": "subtitles", "title": job.get("title") or ""}


@router.get("/export/{plex_guid:path}")
async def export_segments(plex_guid: str, fmt: str = "edl"):
    """Export a title's segments as EDL or MCF for use in other players."""
    from ...importers import edl, mcf

    job = await db.get_scan_job_by_guid(plex_guid)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown title: {plex_guid}")

    segments = await db.get_segments_for_guid(plex_guid)
    if fmt == "edl":
        body = edl.export(segments)
    elif fmt == "mcf":
        body = mcf.export(segments, title=job.get("title") or "", year=job.get("year"))
    else:
        raise HTTPException(status_code=422, detail=f"Unsupported export format: {fmt}")

    return {"format": fmt, "body": body, "count": len(segments)}
