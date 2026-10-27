"""scan_video with the SigLIP fallback (issue #83): when it runs and what it writes."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

import cleanplex.scanner as scanner
from cleanplex import database as db
from cleanplex.config import Config

pytestmark = pytest.mark.usefixtures("setup_db")

STEP = 1000


async def _job(tmp_path):
    media = tmp_path / "m.mkv"
    media.write_bytes(b"x")
    await db.upsert_scan_job(plex_guid="g", title="T", file_path=str(media), rating_key="",
                             library_id="lib", library_title="Lib")
    await db.set_force_scan("g", True)


async def _scan(nudenet_hits: set[int], siglip_hits: set[int], frames=20, enabled=True):
    """Run scan_video over `frames` fake frames; return (segments, frames SigLIP saw)."""
    seen: list[int] = []

    async def frames_gen(path, step_ms, duration_ms, size):
        for i in range(frames):
            yield i * STEP, f"frame-{i}".encode()

    def classify(jpeg, *args):
        i = int(jpeg.decode().split("-")[1])
        return (i in nudenet_hits, 0.9 if i in nudenet_hits else 0.0, ["FEMALE_BREAST_EXPOSED"] if i in nudenet_hits else [])

    def siglip(jpeg, labels):
        i = int(jpeg.decode().split("-")[1])
        seen.append(i)
        return (0.8, "BUTTOCKS_EXPOSED") if i in siglip_hits else (0.1, "FEMALE_BREAST_EXPOSED")

    config = replace(Config(), scan_step_ms=STEP, segment_gap_ms=1500, segment_min_hits=1,
                     siglip_enabled=enabled, siglip_threshold=0.5, siglip_min_hits=2)
    with patch.object(scanner, "extract_frames_batch", frames_gen), \
         patch.object(scanner, "get_duration_ms", AsyncMock(return_value=frames * STEP)), \
         patch.object(scanner, "_classify_frame", classify), \
         patch.object(scanner.siglip_detector, "score_frame", siglip), \
         patch.object(scanner.siglip_detector, "ensure_models_async", AsyncMock(return_value=True)), \
         patch.object(scanner, "THUMBNAILS_DIR", scanner.Path(db.get_db_path()).parent / "thumbs"):
        await scanner.scan_video("g", config)
    return await db.get_segments_for_guid("g"), seen


async def test_siglip_never_sees_frames_nudenet_flagged(tmp_path):
    await _job(tmp_path)

    _, seen = await _scan(nudenet_hits={3, 4}, siglip_hits=set())

    assert 3 not in seen and 4 not in seen
    assert len(seen) == 18


async def test_two_consecutive_siglip_frames_make_a_medium_siglip_segment(tmp_path):
    await _job(tmp_path)

    segs, _ = await _scan(nudenet_hits=set(), siglip_hits={10, 11})

    assert len(segs) == 1
    assert segs[0]["source"] == "siglip"
    assert segs[0]["severity"] == "medium"
    # Tagged with the winning existing label, never a SigLIP-specific one.
    assert segs[0]["labels"] == "BUTTOCKS_EXPOSED"
    assert segs[0]["detector"] == "siglip-base-224"
    assert segs[0]["confidence"] == pytest.approx(0.8)
    assert segs[0]["start_ms"] == 10 * STEP


async def test_a_lone_siglip_frame_is_dropped(tmp_path):
    await _job(tmp_path)

    segs, _ = await _scan(nudenet_hits=set(), siglip_hits={5, 12})

    assert segs == []


async def test_cluster_with_a_nudenet_hit_stays_a_scanner_segment(tmp_path):
    await _job(tmp_path)

    segs, _ = await _scan(nudenet_hits={6}, siglip_hits={5, 7})

    assert len(segs) == 1
    assert segs[0]["source"] == "scanner"
    assert segs[0]["severity"] == "high"
    assert segs[0]["labels"] == "FEMALE_BREAST_EXPOSED"
    assert segs[0]["start_ms"] == 5 * STEP
    # Records the NudeNet model in use (#92); Config() defaults to 320n.
    assert segs[0]["detector"] == "nudenet-320n"


async def test_disabled_siglip_is_never_called(tmp_path):
    await _job(tmp_path)

    segs, seen = await _scan(nudenet_hits=set(), siglip_hits={10, 11}, enabled=False)

    assert seen == [] and segs == []


def test_segment_source_rules():
    assert scanner._segment_source(1, 1, 0, 1, 2) == "scanner"
    assert scanner._segment_source(1, 1, 0, 2, 2) is None
    assert scanner._segment_source(0, 3, 2, 1, 2) == "siglip"
    assert scanner._segment_source(0, 3, 1, 1, 2) is None
