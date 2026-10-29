"""Per-label confidence thresholds (#120) and the credits cut-off (#121) in the scanner."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import cleanplex.scanner as scanner
from cleanplex import database as db
from cleanplex.config import Config

ENABLED = {"FEMALE_BREAST_EXPOSED", "MALE_GENITALIA_EXPOSED", "BUTTOCKS_EXPOSED"}


def _classify(detections, threshold, label_thresholds=None):
    detector = MagicMock()
    detector.detect.return_value = [{"class": c, "score": s} for c, s in detections]
    with patch.object(scanner, "_get_detector", return_value=detector):
        return scanner._classify_frame(b"jpg", threshold, ENABLED, "320n", "", label_thresholds)


def test_label_without_override_uses_global_threshold():
    nude, score, labels = _classify([("FEMALE_BREAST_EXPOSED", 0.65)], 0.6)
    assert (nude, labels) == (True, ["FEMALE_BREAST_EXPOSED"])
    assert score == pytest.approx(0.65)


def test_override_above_score_rejects_a_label_the_global_threshold_would_pass():
    nude, _, labels = _classify([("MALE_GENITALIA_EXPOSED", 0.7)], 0.6, {"MALE_GENITALIA_EXPOSED": 0.8})
    assert nude is False and labels == []


def test_override_below_global_lets_a_label_through():
    nude, _, labels = _classify([("BUTTOCKS_EXPOSED", 0.45)], 0.6, {"BUTTOCKS_EXPOSED": 0.4})
    assert nude is True and labels == ["BUTTOCKS_EXPOSED"]


def test_passing_label_does_not_drag_in_a_label_below_its_own_threshold():
    nude, score, labels = _classify(
        [("FEMALE_BREAST_EXPOSED", 0.9), ("MALE_GENITALIA_EXPOSED", 0.7)],
        0.6,
        {"MALE_GENITALIA_EXPOSED": 0.8},
    )
    assert nude is True and labels == ["FEMALE_BREAST_EXPOSED"]
    assert score == pytest.approx(0.9)


def test_disabled_label_never_counts_even_with_an_override():
    nude, _, labels = _classify([("FEET_EXPOSED", 0.99)], 0.6, {"FEET_EXPOSED": 0.1})
    assert nude is False and labels == []


def test_config_threshold_helpers_inherit_the_global_value():
    cfg = replace(Config(), confidence_threshold=0.6, siglip_threshold=0.3,
                  label_thresholds={"BUTTOCKS_EXPOSED": 0.8},
                  siglip_label_thresholds={"MALE_GENITALIA_EXPOSED": 0.9})
    assert cfg.threshold_for("BUTTOCKS_EXPOSED") == 0.8
    assert cfg.threshold_for("FEMALE_BREAST_EXPOSED") == 0.6
    assert cfg.siglip_threshold_for("MALE_GENITALIA_EXPOSED") == 0.9
    assert cfg.siglip_threshold_for("BUTTOCKS_EXPOSED") == 0.3


@pytest.mark.usefixtures("setup_db")
async def test_config_load_parses_threshold_maps_and_ignores_garbage():
    await db.set_setting("label_thresholds", '{"BUTTOCKS_EXPOSED": 0.7, "BAD": "x"}')
    await db.set_setting("siglip_label_thresholds", "not json")
    cfg = await Config.load()
    assert cfg.label_thresholds == {"BUTTOCKS_EXPOSED": 0.7}
    assert cfg.siglip_label_thresholds == {}
    assert cfg.ignore_credits is True


# ── credits marker ──────────────────────────────────────────────────────────────

def _client(markers=None, error=None):
    client = MagicMock()
    client.get_markers = AsyncMock(side_effect=error) if error else AsyncMock(return_value=markers or [])
    return client


async def _start(markers=None, error=None):
    with patch.object(scanner.plex_mod, "get_client", return_value=_client(markers, error)):
        return await scanner._credits_start_ms("1")


async def test_credits_start_is_none_without_a_credits_marker():
    assert await _start([{"marker_type": "intro", "start_ms": 5, "final": False}]) is None


async def test_credits_start_prefers_a_final_marker():
    markers = [
        {"marker_type": "credits", "start_ms": 100, "final": True},
        {"marker_type": "credits", "start_ms": 900, "final": False},
    ]
    assert await _start(markers) == 100


async def test_credits_start_falls_back_to_latest_credits_marker():
    markers = [{"marker_type": "credits", "start_ms": 100}, {"marker_type": "credits", "start_ms": 900}]
    assert await _start(markers) == 900


async def test_credits_start_swallows_plex_errors():
    assert await _start(error=RuntimeError("plex down")) is None


# ── scan_video ──────────────────────────────────────────────────────────────────

STEP = 1000


async def _scan(credits_at, *, ignore_credits=True, frames=20, siglip_hits=frozenset(range(20)),
                siglip_label="BUTTOCKS_EXPOSED", siglip_score=0.8, siglip_label_thresholds=None):
    media_guid = "g"
    seen: list[int] = []

    async def frames_gen(path, step_ms, duration_ms, size, hardware=False):
        for i in range(frames):
            yield i * STEP, f"frame-{i}".encode()

    def siglip(jpeg, labels):
        i = int(jpeg.decode().split("-")[1])
        seen.append(i)
        return (siglip_score, siglip_label) if i in siglip_hits else (0.1, siglip_label)

    config = replace(Config(), scan_step_ms=STEP, segment_gap_ms=1500, segment_min_hits=1,
                     siglip_enabled=True, siglip_threshold=0.5, siglip_min_hits=2,
                     ignore_credits=ignore_credits,
                     siglip_label_thresholds=siglip_label_thresholds or {})
    with patch.object(scanner, "extract_frames_batch", frames_gen), \
         patch.object(scanner, "get_duration_ms", AsyncMock(return_value=frames * STEP)), \
         patch.object(scanner, "_classify_frame", lambda *a: (False, 0.0, [])), \
         patch.object(scanner, "_credits_start_ms", AsyncMock(return_value=credits_at)), \
         patch.object(scanner.siglip_detector, "score_frame", siglip), \
         patch.object(scanner.siglip_detector, "ensure_models_async", AsyncMock(return_value=True)), \
         patch.object(scanner, "THUMBNAILS_DIR", scanner.Path(db.get_db_path()).parent / "thumbs"):
        await scanner.scan_video(media_guid, config)
    return await db.get_segments_for_guid(media_guid), seen


async def _job(tmp_path, rating_key="42"):
    media = tmp_path / "m.mkv"
    media.write_bytes(b"x")
    await db.upsert_scan_job(plex_guid="g", title="T", file_path=str(media), rating_key=rating_key,
                             library_id="lib", library_title="Lib")
    await db.set_force_scan("g", True)


@pytest.mark.usefixtures("setup_db")
async def test_siglip_is_not_run_on_frames_at_or_after_credits(tmp_path):
    await _job(tmp_path)
    segs, seen = await _scan(credits_at=12 * STEP)
    assert max(seen) == 11
    assert all(seg["end_ms"] <= 14 * STEP for seg in segs)


@pytest.mark.usefixtures("setup_db")
async def test_credits_are_not_ignored_when_the_setting_is_off(tmp_path):
    await _job(tmp_path)
    _, seen = await _scan(credits_at=12 * STEP, ignore_credits=False)
    assert 19 in seen


@pytest.mark.usefixtures("setup_db")
async def test_unknown_credits_leave_scanning_unchanged(tmp_path):
    await _job(tmp_path)
    _, seen = await _scan(credits_at=None)
    assert len(seen) == 20


@pytest.mark.usefixtures("setup_db")
async def test_siglip_hit_is_judged_by_its_tagged_labels_threshold(tmp_path):
    await _job(tmp_path)
    segs, _ = await _scan(credits_at=None, siglip_label_thresholds={"BUTTOCKS_EXPOSED": 0.9})
    assert segs == []


@pytest.mark.usefixtures("setup_db")
async def test_siglip_label_without_override_uses_the_global_threshold(tmp_path):
    await _job(tmp_path)
    segs, _ = await _scan(credits_at=None, siglip_label_thresholds={"MALE_GENITALIA_EXPOSED": 0.95})
    assert len(segs) == 1
