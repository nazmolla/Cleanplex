"""Analytics routes: label-based segment statistics and segment browsing."""

from urllib.parse import quote

from fastapi import APIRouter, Query

from ... import database as db

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


def _plex_image_proxy_url(path: str) -> str:
    if not path:
        return ""
    return f"/api/plex-image?path={quote(path, safe='')}"


@router.get("/labels")
async def get_label_counts():
    """Return segment counts grouped by label, descending by count."""
    data = await db.get_segment_counts_by_label()
    return {"labels": data}


@router.get("/labels/{label}/ratings")
async def get_label_rating_counts(label: str):
    """Return segment counts for a specific label grouped by content rating."""
    data = await db.get_segment_counts_by_rating_for_label(label)
    return {"label": label, "ratings": data}


@router.get("/segments")
async def get_segments_by_labels(
    labels: str = Query(..., description="Comma-separated label names"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Return segments matching any of the given labels, with poster proxy URLs."""
    label_list = [l.strip() for l in labels.split(",") if l.strip()]
    if not label_list:
        return {"segments": [], "total": 0}

    rows, total = await _fetch_segments_and_total(label_list, limit, offset)
    return {"segments": rows, "total": total}


async def _fetch_segments_and_total(
    labels: list[str], limit: int, offset: int
) -> tuple[list[dict], int]:
    segs, total = await _parallel_fetch(labels, limit, offset)
    result = []
    for seg in segs:
        rating_key = seg.get("rating_key") or ""
        show_rating_key = seg.get("show_rating_key") or ""
        media_type = seg.get("media_type") or "movie"

        # Use the same poster-URL logic as get_titles_in_library: show poster
        # for episodes (via show_rating_key), episode thumb for movies.
        if media_type == "episode" and show_rating_key:
            poster_url = _plex_image_proxy_url(f"/library/metadata/{show_rating_key}/thumb")
        elif rating_key:
            poster_url = _plex_image_proxy_url(f"/library/metadata/{rating_key}/thumb")
        else:
            poster_url = ""

        result.append({
            "id": seg["id"],
            "plex_guid": seg["plex_guid"],
            "title": seg["title"],
            "start_ms": seg["start_ms"],
            "end_ms": seg["end_ms"],
            "confidence": seg["confidence"],
            "labels": seg.get("labels") or "",
            "has_thumbnail": bool(seg.get("thumbnail_path")),
            "thumbnail_url": f"/api/thumbnails/{seg['id']}" if seg.get("thumbnail_path") else "",
            "poster_url": poster_url,
            "content_rating": seg.get("content_rating") or "",
            "media_type": media_type,
            "rating_key": rating_key,
        })
    return result, total


async def _parallel_fetch(labels: list[str], limit: int, offset: int):
    import asyncio
    segs, total = await asyncio.gather(
        db.get_segments_for_labels(labels, limit, offset),
        db.count_segments_for_labels(labels),
    )
    return segs, total
