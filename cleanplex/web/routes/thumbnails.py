from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ... import database as db

router = APIRouter(prefix="/api/thumbnails", tags=["thumbnails"])

# Thumbnail paths are immutable — once a segment is created its thumbnail_path never
# changes, so we cache segment_id → path forever (process lifetime).
# A missing/deleted file still returns 404 via the is_file() check.
_thumbnail_path_cache: dict[int, str] = {}

# Thumbnails are immutable: cache them in the browser for 1 year.
_THUMB_CACHE_HEADER = "public, max-age=31536000, immutable"


@router.get("/{segment_id}")
async def get_thumbnail(segment_id: int):
    if segment_id in _thumbnail_path_cache:
        thumb_path = _thumbnail_path_cache[segment_id]
    else:
        seg = await db.get_segment_by_id(segment_id)
        if not seg:
            raise HTTPException(status_code=404, detail="Segment not found")
        thumb_path = seg.get("thumbnail_path") or ""
        if thumb_path:
            _thumbnail_path_cache[segment_id] = thumb_path

    if not thumb_path or not Path(thumb_path).is_file():
        raise HTTPException(status_code=404, detail="Thumbnail not available")

    return FileResponse(
        thumb_path,
        media_type="image/jpeg",
        headers={"Cache-Control": _THUMB_CACHE_HEADER},
    )
