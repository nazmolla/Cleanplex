"""Backfill of thumbnails and bounds for segments stored before dense sampling (#141)."""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from cleanplex import database as db
from cleanplex import hover_previews, scanner, thumbnail_backfill
from cleanplex.config import Config

pytestmark = pytest.mark.usefixtures("setup_db")

GAP = 3000
STEP = 2000


def _t(jpeg: bytes) -> int:
    return int(jpeg.decode().split("-")[1])


@pytest.fixture
def cfg():
    return replace(Config(), scan_step_ms=STEP, segment_gap_ms=GAP, refine_step_ms=500, confidence_threshold=0.7,
                   siglip_enabled=False)


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(scanner, "THUMBNAILS_DIR", tmp_path / "thumbs")
    monkeypatch.setattr(hover_previews, "HOVER_DIR", tmp_path / "hover")
    monkeypatch.setattr(thumbnail_backfill.plex_summary, "refresh_soon", lambda guid: None)


async def _segment(tmp_path, *, start=10_000, end=15_000, conf=0.75, source="scanner", labels="BUTTOCKS_EXPOSED",
                   parts=1, with_file=True, thumb_bytes=b"old-thumb"):
    media = tmp_path / "m.mkv"
    if with_file:
        media.write_bytes(b"x")
    await db.upsert_scan_job(plex_guid="g", title="T", file_path=str(media), rating_key="7", library_id="l",
                             library_title="L", **({"part_files": json.dumps([str(media)] * parts)} if parts > 1 else {}))
    thumb = tmp_path / "old.jpg"
    thumb.write_bytes(thumb_bytes)
    sid = await db.insert_segment("g", "T", start, end, confidence=conf, labels=labels, severity="medium",
                                  thumbnail_path=str(thumb), source=source,
                                  detector="siglip-base-224" if source == "siglip" else "nudenet-320n")
    return sid, thumb


class Film:
    def __init__(self, nudenet=None, siglip=None, length_ms=60_000):
        self.nudenet = nudenet or (lambda t: 0.1)
        self.siglip = siglip or (lambda t: 0.1)
        self.length_ms = length_ms
        self.windows = []

    async def window(self, path, start_ms, length_ms, step_ms, size=320, hardware=False):
        self.windows.append((start_ms, length_ms, step_ms))
        t = start_ms
        while t < start_ms + length_ms and t <= self.length_ms:
            yield t, f"frame-{t}".encode()
            t += step_ms

    def classify(self, jpeg, threshold, enabled, model, path, label_thresholds=None):
        score = self.nudenet(_t(jpeg))
        return (score >= threshold, score, ["FEMALE_BREAST_EXPOSED"] if score >= threshold else [])

    def siglip_score(self, jpeg, labels):
        return (self.siglip(_t(jpeg)), "BUTTOCKS_EXPOSED")


async def _run(film, cfg, *, siglip_on=False, **kw):
    detectors = ("320n", "", False, siglip_on)
    with patch.object(thumbnail_backfill, "extract_frames_window", film.window), \
         patch.object(thumbnail_backfill, "get_duration_ms", AsyncMock(return_value=film.length_ms)), \
         patch.object(scanner, "_classify_frame", film.classify), \
         patch.object(scanner.siglip_detector, "score_frame", film.siglip_score), \
         patch.object(scanner, "_credits_start_ms", AsyncMock(return_value=None)):
        pending = await db.get_segments_to_refine(10)
        return [await thumbnail_backfill.refine_segment(s, cfg, detectors, {}, {}) for s in pending]


async def test_the_peak_frame_becomes_the_thumbnail_and_the_bounds_grow(tmp_path, cfg):
    sid, thumb = await _segment(tmp_path)
    film = Film(nudenet=lambda t: {9_500: 0.8, 11_000: 0.95, 12_000: 0.8, 13_500: 0.75}.get(t, 0.1))

    assert await _run(film, cfg) == ["refined"]

    seg = await db.get_segment_by_id(sid)
    assert thumb.read_bytes() == b"frame-11000"
    assert seg["thumbnail_ms"] == 11_000
    assert seg["confidence"] == pytest.approx(0.95)
    assert seg["start_ms"] == 9_500
    assert seg["end_ms"] == 13_500 + GAP
    assert seg["refined_at"] is not None


async def test_labels_found_in_the_window_are_added_and_severity_follows(tmp_path, cfg):
    sid, _ = await _segment(tmp_path, labels="BUTTOCKS_EXPOSED")
    film = Film(nudenet=lambda t: 0.9 if t == 11_000 else 0.1)

    await _run(film, cfg)

    seg = await db.get_segment_by_id(sid)
    assert seg["labels"] == "BUTTOCKS_EXPOSED,FEMALE_BREAST_EXPOSED"
    assert seg["severity"] == "medium"


async def test_a_window_with_no_hits_is_marked_refined_and_otherwise_left_alone(tmp_path, cfg):
    sid, thumb = await _segment(tmp_path)

    assert await _run(Film(), cfg) == ["unchanged"]

    seg = await db.get_segment_by_id(sid)
    assert thumb.read_bytes() == b"old-thumb" and seg["thumbnail_ms"] is None
    assert (seg["start_ms"], seg["end_ms"], seg["confidence"]) == (10_000, 15_000, 0.75)
    assert seg["refined_at"] is not None
    assert await db.count_segments_to_refine() == 0


async def test_a_weaker_peak_keeps_the_old_thumbnail_and_confidence_never_drops(tmp_path, cfg):
    sid, thumb = await _segment(tmp_path, conf=0.9)
    film = Film(nudenet=lambda t: 0.75 if t in (11_000, 18_000) else 0.1)

    await _run(film, cfg)

    seg = await db.get_segment_by_id(sid)
    assert thumb.read_bytes() == b"old-thumb" and seg["thumbnail_ms"] is None
    assert seg["confidence"] == pytest.approx(0.9)
    assert seg["end_ms"] >= 15_000  # bounds may still grow


async def test_bounds_never_shrink(tmp_path, cfg):
    sid, _ = await _segment(tmp_path, start=10_000, end=30_000)
    await _run(Film(nudenet=lambda t: 0.9 if t == 12_000 else 0.1), cfg)
    seg = await db.get_segment_by_id(sid)
    assert seg["start_ms"] <= 10_000 and seg["end_ms"] >= 30_000


async def test_a_segment_whose_window_changed_gets_its_hover_preview_reset(tmp_path, cfg):
    sid, _ = await _segment(tmp_path)
    await db.set_hover_status(sid, "done")
    hover_previews.HOVER_DIR.mkdir(parents=True, exist_ok=True)
    hover_previews.hover_path(sid).write_bytes(b"clip")

    await _run(Film(nudenet=lambda t: 0.9 if t in (9_000, 11_000) else 0.1), cfg)

    seg = await db.get_segment_by_id(sid)
    assert seg["hover_status"] == ""
    assert not hover_previews.hover_path(sid).exists()


async def test_a_segment_whose_window_is_unchanged_keeps_its_hover_preview(tmp_path, cfg):
    sid, _ = await _segment(tmp_path, start=10_000, end=20_000)
    await db.set_hover_status(sid, "done")
    hover_previews.HOVER_DIR.mkdir(parents=True, exist_ok=True)
    hover_previews.hover_path(sid).write_bytes(b"clip")

    await _run(Film(nudenet=lambda t: 0.9 if t == 12_000 else 0.1), cfg)

    assert (await db.get_segment_by_id(sid))["hover_status"] == "done"
    assert hover_previews.hover_path(sid).exists()


async def test_a_siglip_segment_with_a_nudenet_hit_becomes_a_scanner_segment(tmp_path, cfg):
    sid, _ = await _segment(tmp_path, source="siglip", conf=0.8)
    film = Film(nudenet=lambda t: 0.9 if t == 11_000 else 0.1, siglip=lambda t: 0.6)

    await _run(film, replace(cfg, siglip_enabled=True, siglip_threshold=0.5), siglip_on=True)

    seg = await db.get_segment_by_id(sid)
    assert seg["source"] == "scanner" and seg["detector"] == "nudenet-320n"
    assert seg["confidence"] == pytest.approx(0.9)
    assert seg["thumbnail_ms"] == 11_000


async def test_a_siglip_only_segment_stays_siglip_with_an_unoffset_confidence(tmp_path, cfg):
    sid, thumb = await _segment(tmp_path, source="siglip", conf=0.6)
    film = Film(siglip=lambda t: 0.9 if t == 11_000 else 0.1)

    await _run(film, replace(cfg, siglip_enabled=True, siglip_threshold=0.5), siglip_on=True)

    seg = await db.get_segment_by_id(sid)
    assert seg["source"] == "siglip" and seg["confidence"] == pytest.approx(0.9)
    assert seg["confidence"] <= 1.0
    assert thumb.read_bytes() == b"frame-11000"


async def test_a_scanner_segment_is_not_refined_by_siglip(tmp_path, cfg):
    sid, _ = await _segment(tmp_path, source="scanner", conf=0.75)
    film = Film(siglip=lambda t: 0.99)

    await _run(film, replace(cfg, siglip_enabled=True, siglip_threshold=0.5), siglip_on=True)

    assert (await db.get_segment_by_id(sid))["end_ms"] == 15_000


async def test_multi_part_titles_are_skipped_and_not_retried(tmp_path, cfg):
    sid, _ = await _segment(tmp_path, parts=2)
    assert await _run(Film(nudenet=lambda t: 0.9), cfg) == ["skipped"]
    assert (await db.get_segment_by_id(sid))["refined_at"] is not None


async def test_an_unreachable_file_stays_pending_for_a_later_run(tmp_path, cfg):
    sid, _ = await _segment(tmp_path, with_file=False)
    assert await _run(Film(), cfg) == ["offline"]
    assert (await db.get_segment_by_id(sid))["refined_at"] is None
    assert await db.count_segments_to_refine() == 1


async def test_only_active_detector_segments_without_a_thumbnail_time_are_candidates(tmp_path):
    sid, _ = await _segment(tmp_path)
    done = await db.insert_segment("g", "T", 100, 200, source="scanner", thumbnail_ms=150)
    imported = await db.insert_segment("g", "T", 300, 400, source="skp")
    wrong = await db.insert_segment("g", "T", 500, 600, source="scanner")
    await db.mark_segment_false_positive(wrong)

    ids = [s["id"] for s in await db.get_segments_to_refine(10)]

    assert ids == [sid]
    assert done and imported


async def test_a_run_looks_at_each_segment_once_and_does_not_loop_on_unreachable_ones(tmp_path, cfg):
    await _segment(tmp_path, with_file=False)

    async def get_config():
        return cfg

    async def always():
        return True

    with patch.object(scanner, "prepare_detectors", AsyncMock(return_value=("320n", "", False, False))):
        looked = await thumbnail_backfill.refine_pending(get_config, always)

    assert looked == 1


async def test_a_run_stops_when_the_window_closes(tmp_path, cfg):
    for i in range(3):
        await db.upsert_scan_job(plex_guid=f"g{i}", title="T", file_path=str(tmp_path / "m.mkv"), rating_key=str(i),
                                 library_id="l", library_title="L")
        await db.insert_segment(f"g{i}", "T", 100, 200, source="scanner")
    calls = {"n": 0}

    async def get_config():
        return cfg

    async def two_then_stop():
        calls["n"] += 1
        return calls["n"] <= 3

    with patch.object(scanner, "prepare_detectors", AsyncMock(return_value=("320n", "", False, False))), \
         patch.object(thumbnail_backfill, "PAUSE_S", 0):
        looked = await thumbnail_backfill.refine_pending(get_config, two_then_stop)

    assert 0 < looked < 3
