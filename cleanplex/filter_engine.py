"""Filter engine: checks playback position against stored segments and seeks past them."""

from __future__ import annotations

from .logger import get_logger
from . import database as db
from .plex_client import ActiveSession, PlexClient

logger = get_logger(__name__)

# Track recently skipped sessions to avoid re-triggering: {session_key: end_ms}
_recently_skipped: dict[str, int] = {}


async def process(session: ActiveSession, client: PlexClient, skip_buffer_ms: int) -> None:
    """Check the session's position against stored segments and seek if needed."""
    if not session.is_controllable:
        logger.info("Session %s (%s) is not controllable – skipping", session.session_key, session.full_title)
        return

    pos = session.position_ms

    # Don't re-trigger if we already skipped past this point recently
    skip_until = _recently_skipped.get(session.session_key, 0)
    if pos < skip_until:
        return

    segments = await db.get_segments_for_guid(session.plex_guid)

    # Plex session GUIDs can differ from library-scan GUIDs (ordering of guids[] varies).
    # Fall back to rating_key lookup so we still find the right segments.
    if not segments and session.rating_key:
        segments = await db.get_segments_by_rating_key(session.rating_key)
        if segments:
            logger.info(
                "GUID mismatch for '%s': session_guid=%s, found %d segment(s) via rating_key=%s",
                session.full_title, session.plex_guid, len(segments), session.rating_key,
            )

    if not segments:
        logger.info("No segments found for '%s' (guid=%s, rating_key=%s)", session.full_title, session.plex_guid, session.rating_key)
        return

    logger.info("Checking %d segment(s) for '%s' at pos=%dms (client=%s)", len(segments), session.full_title, pos, session.client_identifier)
    for seg in segments:
        if seg["start_ms"] <= pos <= seg["end_ms"]:
            target = seg["end_ms"] + skip_buffer_ms
            logger.info(
                "Skipping [%s] for user '%s': %dms → %dms (segment: %d–%d, confidence=%.2f)",
                session.full_title,
                session.user,
                pos,
                target,
                seg["start_ms"],
                seg["end_ms"],
                seg["confidence"],
            )
            success = await client.seek(
                session.client_identifier,
                target,
                session.client_address,
                session.client_port,
            )
            if success:
                _recently_skipped[session.session_key] = target + 5000
            return

    # Clean up stale entries for sessions no longer in range
    if session.session_key in _recently_skipped and pos > _recently_skipped[session.session_key]:
        del _recently_skipped[session.session_key]
