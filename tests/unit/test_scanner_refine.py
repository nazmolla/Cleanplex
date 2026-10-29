"""Dense re-sampling of each flagged window for its thumbnail and bounds (#133)."""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

import cleanplex.scanner as scanner
from cleanplex import database as db
from cleanplex.config import Config

pytestmark = pytest.mark.usefixtures("setup_db")

STEP = 2000
GAP = 3000


def _t(jpeg: bytes) -> int:
    return int(jpeg.decode().split("-")[1])


async def _job(tmp_path, parts=1):
    files = []
    for i in range(parts):
        f = tmp_path / f"m{i}.mkv"
        f.write_bytes(b"x")
        files.append(str(f))
    await db.upsert_scan_job(plex_guid="g", title="T", file_path=files[0], rating_key="",
                             library_id="lib", library_title="Lib",
                             **({"part_files": json.dumps(files)} if parts > 1 else {}))
    await db.set_force_scan("g", True)


class Film:
    """A fake film: `score_at(t_ms)` is what NudeNet would score the frame at t_ms."""

    def __init__(self, score_at, length_ms=60_000, parts=1):
        self.score_at = score_at
        self.length_ms = length_ms
        self.parts = parts
        self.windows: list[tuple[str, int, int, int]] = []

    async def batch(self, path, step_ms, duration_ms, size, hardware=False):
        for t in range(0, duration_ms + 1, step_ms):
            yield t, f"frame-{t}".encode()

    async def window(self, path, start_ms, length_ms, step_ms, size=320, hardware=False):
        self.windows.append((path, start_ms, length_ms, step_ms))
        t = start_ms
        while t < start_ms + length_ms and t <= self.length_ms:
            yield t, f"frame-{t}".encode()
            t += step_ms

    def classify_for(self, offset_of_part):
        def classify(jpeg, threshold, enabled, model, path, label_thresholds=None):
            t = _t(jpeg)
            score = self.score_at(t)
            return (score >= threshold, score, ["FEMALE_BREAST_EXPOSED"] if score >= threshold else [])
        return classify


async def _scan(film, tmp_path, *, refine_ms=500, parts=1, **cfg):
    config = replace(Config(), scan_step_ms=STEP, segment_gap_ms=GAP, segment_min_hits=1, siglip_enabled=False,
                     confidence_threshold=0.7, refine_step_ms=refine_ms, **cfg)
    thumbs = scanner.Path(db.get_db_path()).parent / "thumbs"
    with patch.object(scanner, "extract_frames_batch", film.batch), \
         patch.object(scanner, "extract_frames_window", film.window), \
         patch.object(scanner, "get_duration_ms", AsyncMock(return_value=film.length_ms)), \
         patch.object(scanner, "_classify_frame", film.classify_for(0)), \
         patch.object(scanner, "THUMBNAILS_DIR", thumbs):
        await scanner.scan_video("g", config)
    return await db.get_segments_for_guid("g"), thumbs


def _scores(table, default=0.1):
    return lambda t: table.get(t, default)


async def test_the_thumbnail_is_the_peak_frame_between_samples(tmp_path):
    await _job(tmp_path)
    film = Film(_scores({10_000: 0.75, 11_000: 0.95, 11_500: 0.9, 12_000: 0.8, 12_500: 0.72}))

    segs, thumbs = await _scan(film, tmp_path)

    assert len(segs) == 1
    seg = segs[0]
    assert seg["thumbnail_ms"] == 11_000
    assert seg["confidence"] == pytest.approx(0.95)
    assert (thumbs / "g_10000.jpg").read_bytes() == b"frame-11000"


async def test_the_segment_starts_at_the_first_dense_hit(tmp_path):
    await _job(tmp_path)
    film = Film(_scores({9_500: 0.8, 10_000: 0.75, 12_000: 0.8}))

    segs, _ = await _scan(film, tmp_path)

    assert segs[0]["start_ms"] == 9_500


async def test_the_end_follows_a_scene_that_runs_on_between_samples(tmp_path):
    await _job(tmp_path)
    # Coarse samples (multiples of 2 s) miss it; the 0.5 s grid sees the scene until 17 s.
    film = Film(lambda t: 0.8 if (10_000 <= t <= 12_000 or (12_000 < t <= 17_000 and t % STEP)) else 0.1)

    segs, _ = await _scan(film, tmp_path)

    assert segs[0]["end_ms"] == 17_000 + GAP


async def test_growth_stops_after_one_merge_gap(tmp_path):
    await _job(tmp_path)
    film = Film(lambda t: 0.8 if (10_000 <= t <= 12_000 or (t > 12_000 and t % STEP)) else 0.1, length_ms=120_000)

    segs, _ = await _scan(film, tmp_path)

    # A scene that never ends is cut off: 2 s window + at most one gap of growth past it.
    assert segs[0]["end_ms"] <= 12_000 + STEP + GAP + 2 * STEP + GAP


async def test_refinement_off_keeps_the_coarse_result_and_never_resamples(tmp_path):
    await _job(tmp_path)
    film = Film(_scores({10_000: 0.75, 11_000: 0.95, 12_000: 0.8}))

    segs, _ = await _scan(film, tmp_path, refine_ms=0)

    assert film.windows == []
    assert segs[0]["thumbnail_ms"] == 12_000 and segs[0]["start_ms"] == 10_000


async def test_the_window_is_bounded_by_the_file(tmp_path):
    await _job(tmp_path)
    film = Film(_scores({0: 0.9, 2_000: 0.9}), length_ms=10_000)

    segs, _ = await _scan(film, tmp_path)

    assert all(start >= 0 for _, start, _, _ in film.windows)
    assert segs[0]["start_ms"] == 0


async def test_a_long_segment_is_sampled_with_a_bounded_number_of_frames(tmp_path):
    await _job(tmp_path)
    film = Film(lambda t: 0.9 if 2_000 <= t <= 298_000 else 0.1, length_ms=300_000)

    await _scan(film, tmp_path)

    path, start, length, dstep = film.windows[0]
    assert length // dstep <= scanner.REFINE_MAX_FRAMES + 1
    assert dstep > 500


async def test_a_failing_window_leaves_the_coarse_segment(tmp_path):
    await _job(tmp_path)
    film = Film(_scores({10_000: 0.75, 12_000: 0.8}))

    async def boom(*args, **kwargs):
        raise RuntimeError("ffmpeg died")
        yield  # pragma: no cover

    film.window = boom
    segs, _ = await _scan(film, tmp_path)

    assert len(segs) == 1
    assert (segs[0]["start_ms"], segs[0]["thumbnail_ms"]) == (10_000, 12_000)


async def test_a_dense_peak_never_drops_a_segment_the_coarse_pass_found(tmp_path):
    await _job(tmp_path)
    film = Film(_scores({10_000: 0.75}))
    film.window = lambda *a, **k: _empty()

    segs, _ = await _scan(film, tmp_path)

    assert len(segs) == 1 and segs[0]["thumbnail_ms"] == 10_000


async def _empty():
    return
    yield  # pragma: no cover


async def test_second_part_window_is_asked_in_part_time_and_stored_in_film_time(tmp_path):
    await _job(tmp_path, parts=2)
    state = {"part": 0}
    calls: list[tuple[str, int]] = []

    async def batch(path, step_ms, duration_ms, size, hardware=False):
        state["part"] = 1 if path.endswith("m1.mkv") else 0
        for t in range(0, duration_ms + 1, step_ms):
            yield t, f"frame-{t}".encode()

    async def window(path, start_ms, length_ms, step_ms, size=320, hardware=False):
        calls.append((path, start_ms))
        t = start_ms
        while t < start_ms + length_ms and t <= 30_000:
            yield t, f"frame-{t}".encode()
            t += step_ms

    def classify(jpeg, threshold, enabled, model, path, label_thresholds=None):
        # Only part 2's own 10 s mark (film time 40 s) is nudity.
        score = 0.9 if state["part"] == 1 and _t(jpeg) == 10_000 else 0.1
        return (score >= threshold, score, ["FEMALE_BREAST_EXPOSED"] if score >= threshold else [])

    config = replace(Config(), scan_step_ms=STEP, segment_gap_ms=GAP, segment_min_hits=1, siglip_enabled=False,
                     confidence_threshold=0.7, refine_step_ms=500)
    with patch.object(scanner, "extract_frames_batch", batch), \
         patch.object(scanner, "extract_frames_window", window), \
         patch.object(scanner, "get_duration_ms", AsyncMock(return_value=30_000)), \
         patch.object(scanner, "_classify_frame", classify), \
         patch.object(scanner, "THUMBNAILS_DIR", scanner.Path(db.get_db_path()).parent / "thumbs"):
        await scanner.scan_video("g", config)

    segs = await db.get_segments_for_guid("g")
    assert len(segs) == 1
    assert segs[0]["start_ms"] == 40_000  # part 1 is 30 s long, so part time 10 s is film time 40 s
    assert segs[0]["thumbnail_ms"] == 40_000
    assert calls and all(path.endswith("m1.mkv") for path, _ in calls)
    assert min(start for _, start in calls) == 8_000  # one coarse step before the cluster, in part time


# ── SigLIP clusters ───────────────────────────────────────────────────────────────

async def _scan_siglip(film, siglip_score_at, *, nudenet=None, min_hits=1):
    config = replace(Config(), scan_step_ms=STEP, segment_gap_ms=GAP, segment_min_hits=min_hits, confidence_threshold=0.7,
                     refine_step_ms=500, siglip_enabled=True, siglip_threshold=0.5, siglip_min_hits=2)

    def siglip(jpeg, labels):
        return (siglip_score_at(_t(jpeg)), "BUTTOCKS_EXPOSED")

    if nudenet is not None:
        film.score_at = nudenet
    with patch.object(scanner, "extract_frames_batch", film.batch), \
         patch.object(scanner, "extract_frames_window", film.window), \
         patch.object(scanner, "get_duration_ms", AsyncMock(return_value=film.length_ms)), \
         patch.object(scanner, "_classify_frame", film.classify_for(0)), \
         patch.object(scanner.siglip_detector, "score_frame", siglip), \
         patch.object(scanner.siglip_detector, "ensure_models_async", AsyncMock(return_value=True)), \
         patch.object(scanner, "THUMBNAILS_DIR", scanner.Path(db.get_db_path()).parent / "thumbs"):
        await scanner.scan_video("g", config)
    return await db.get_segments_for_guid("g")


async def test_a_siglip_segment_is_refined_with_siglip(tmp_path):
    await _job(tmp_path)
    film = Film(_scores({}))

    segs = await _scan_siglip(film, _scores({10_000: 0.6, 11_000: 0.95, 12_000: 0.7}, default=0.1))

    assert len(segs) == 1 and segs[0]["source"] == "siglip"
    assert segs[0]["thumbnail_ms"] == 11_000
    assert segs[0]["confidence"] == pytest.approx(0.95)


async def test_siglip_only_dense_hits_do_not_stretch_a_nudenet_segment(tmp_path):
    await _job(tmp_path)
    film = Film(_scores({10_000: 0.9}))

    segs = await _scan_siglip(film, lambda t: 0.8 if 12_500 <= t <= 14_000 else 0.1)

    assert len(segs) == 1 and segs[0]["source"] == "scanner"
    assert segs[0]["end_ms"] == 10_000 + GAP
    assert segs[0]["thumbnail_ms"] == 10_000


async def test_a_siglip_cluster_with_dense_nudenet_hits_becomes_a_scanner_segment(tmp_path):
    await _job(tmp_path)
    film = Film(_scores({}))

    # SigLIP sees the scene at 10-12 s; only the 0.5 s grid finds NudeNet agreeing at 11 s.
    segs = await _scan_siglip(film, _scores({10_000: 0.6, 12_000: 0.7}, default=0.1),
                              nudenet=_scores({11_000: 0.9}))

    assert len(segs) == 1
    seg = segs[0]
    assert seg["source"] == "scanner" and seg["detector"].startswith("nudenet")
    assert seg["thumbnail_ms"] == 11_000
    assert seg["confidence"] == pytest.approx(0.9)  # a NudeNet score, not a SigLIP one offset by +1


async def test_dense_nudenet_hits_are_ignored_when_they_cannot_promote_the_cluster(tmp_path):
    await _job(tmp_path)
    film = Film(_scores({}))
    config_min_hits = 4  # the scanner rule would need 4 hits; the cluster has 2 SigLIP + 1 dense

    def run():
        return _scan_siglip(film, _scores({10_000: 0.6, 12_000: 0.7}, default=0.1),
                            nudenet=_scores({11_000: 0.9}), min_hits=config_min_hits)

    segs = await run()

    assert len(segs) == 1
    assert segs[0]["source"] == "siglip"
    assert segs[0]["confidence"] <= 1.0
    assert segs[0]["thumbnail_ms"] in (10_000, 12_000)
