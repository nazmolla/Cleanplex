"""Analytics routes: label-based segment statistics and segment browsing."""

from datetime import datetime
from typing import Literal
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
    sort: Literal["score", "added"] = Query("score", description="score = confidence, added = date added"),
    order: Literal["asc", "desc"] = Query("desc"),
):
    """Return segments matching any of the given labels, with poster proxy URLs."""
    label_list = [l.strip() for l in labels.split(",") if l.strip()]
    if not label_list:
        return {"segments": [], "total": 0}

    rows, total = await _fetch_segments_and_total(label_list, limit, offset, sort, order)
    return {"segments": rows, "total": total}


async def _fetch_segments_and_total(
    labels: list[str], limit: int, offset: int, sort: str = "score", order: str = "desc"
) -> tuple[list[dict], int]:
    segs, total = await _parallel_fetch(labels, limit, offset, sort, order)
    return [_segment_payload(seg) for seg in segs], total


def _segment_payload(seg: dict) -> dict:
    """Shape one segment row (joined with its scan job) for the analytics lists."""
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

    return {
        "id": seg["id"],
        "plex_guid": seg["plex_guid"],
        "title": seg["title"],
        "start_ms": seg["start_ms"],
        "end_ms": seg["end_ms"],
        "confidence": seg["confidence"],
        "labels": seg.get("labels") or "",
        "source": seg.get("source") or "scanner",
        "detector": seg.get("detector") or "",
        "thumbnail_ms": seg.get("thumbnail_ms"),
        "has_thumbnail": bool(seg.get("thumbnail_path")),
        "thumbnail_url": f"/api/thumbnails/{seg['id']}" if seg.get("thumbnail_path") else "",
        "hover_url": f"/api/segments/{seg['id']}/hover" if seg.get("hover_status") == "done" else "",
        "poster_url": poster_url,
        "content_rating": seg.get("content_rating") or "",
        "media_type": media_type,
        "rating_key": rating_key,
    }


async def _parallel_fetch(labels: list[str], limit: int, offset: int, sort: str, order: str):
    import asyncio
    segs, total = await asyncio.gather(
        db.get_segments_for_labels(labels, limit, offset, sort, order),
        db.count_segments_for_labels(labels),
    )
    return segs, total


@router.get("/false-positives")
async def get_false_positives(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    sort: Literal["marked", "score", "added"] = Query("marked", description="marked = when flagged, score = confidence, added = date added"),
    order: Literal["asc", "desc"] = Query("desc"),
    kind: Literal["all", "marked", "verifier"] = Query("all", description="marked = by you, verifier = rejected by the verifier"),
):
    """Return inactive false-positive segments (newest mark first by default), with totals per kind (#165)."""
    import asyncio
    segs, total, marked, verifier = await asyncio.gather(
        db.get_false_positive_segments(limit, offset, sort, order, kind),
        db.count_false_positive_segments(kind),
        db.count_false_positive_segments("marked"),
        db.count_false_positive_segments("verifier"),
    )
    rows = [
        {
            **_segment_payload(seg), "false_positive": True, "auto_rejected": seg.get("fp_state") == db.FP_VERIFIER,
            "marked_at": seg.get("marked_at"),
        }
        for seg in segs
    ]
    return {"segments": rows, "total": total, "counts": {"marked": marked, "verifier": verifier}}


@router.get("/detection-accuracy")
async def get_detection_accuracy():
    """Per detector and label: segments found, marked false positives, mean scores."""
    rows = await db.get_detection_accuracy()
    for r in rows:
        # Verifier rejections are not human verdicts, so they stay out of the rate's denominator.
        judged = r["total"] - r["verifier_rejected"]
        r["false_positive_rate"] = round(r["false_positives"] / judged, 3) if judged else 0.0
    return {"rows": rows, "marked": sum(r["false_positives"] for r in rows)}


@router.get("/skips/by-category")
async def get_skip_counts_by_category():
    """Return how many filter actions fired per content category."""
    return {"categories": await db.get_skip_counts_by_category()}


@router.get("/skips/by-client")
async def get_skip_counts_by_client():
    """Return per-client volume, failure rate and average latency, worst first."""
    return {"clients": await db.get_skip_counts_by_client()}


@router.get("/skips/top-titles")
async def get_most_skipped_titles(limit: int = Query(20, ge=1, le=100)):
    """Return the titles whose segments fire most often."""
    return {"titles": await db.get_most_skipped_titles(limit)}


@router.get("/skips/history")
async def get_skip_history(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    start: datetime | None = Query(None, description="inclusive lower bound, ISO 8601"),
    end: datetime | None = Query(None, description="exclusive upper bound, ISO 8601"),
):
    """Return filter actions newest first, optionally within [start, end)."""
    return {
        "events": await db.get_skip_events(limit, offset, start, end),
        "total": await db.count_skip_events(start, end),
    }
