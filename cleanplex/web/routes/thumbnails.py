from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ... import database as db

router = APIRouter(prefix="/api/thumbnails", tags=["thumbnails"])


@router.get("/{segment_id}")
async def get_thumbnail(segment_id: int):
    seg = await db.get_segment_by_id(segment_id)
    if not seg:
        raise HTTPException(status_code=404, detail="Segment not found")

    thumb_path = seg.get("thumbnail_path")
    if not thumb_path or not Path(thumb_path).is_file():
        raise HTTPException(status_code=404, detail="Thumbnail not available")

    return FileResponse(thumb_path, media_type="image/jpeg")
