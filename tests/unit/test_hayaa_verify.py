"""Local re-check of pulled Hayaa claims (#222)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cleanplex import database as db
from cleanplex import hayaa_verify as hv
from cleanplex import scanner

pytestmark = pytest.mark.usefixtures("setup_db")
TK = "b" * 64


def _claim(path, **kw):
    return {"title_key": TK, "claim_id": "c1", "plex_guid": "g1", "start_ms": 60_000, "end_ms": 70_000,
            "status": "candidate", "file_path": str(path), "duration_ms": 600_000, "title": "Film", **kw}


@pytest.fixture
def config():
    return SimpleNamespace(hw_decode=False, verifier_loss_pct=10)


@pytest.fixture
async def staged(tmp_path, monkeypatch):
    f = tmp_path / "film.mkv"
    f.write_bytes(b"x")
    await db.upsert_scan_jobs_batch([{"plex_guid": "g1", "title": "Film", "file_path": str(f), "rating_key": "1",
                                      "library_id": "1", "library_title": "M"}])
    await db.replace_hayaa_claims(TK, "g1", [{
        "claim_id": "c1", "start_ms": 60_000, "end_ms": 70_000, "status": "candidate", "basis": "machine",
        "labels": "FEMALE_BREAST_EXPOSED", "category": "nudity", "severity": "medium", "machine": "{}",
        "supporters": 1, "rejecters": 0,
    }])
    monkeypatch.setattr(scanner, "THUMBNAILS_DIR", tmp_path / "thumbs")
    monkeypatch.setattr(scanner, "make_judge", lambda *a, **k: None)
    monkeypatch.setattr(scanner, "frame_size_for", lambda *a: 320)
    return f


async def test_hit_stores_family_confidence_and_a_thumbnail(staged, config, monkeypatch):
    seen = {}

    async def fake_hits(**kw):
        seen.update(kw)
        return [(65_000, True, 0.83, ["FEMALE_BREAST_EXPOSED"], b"jpeg")]

    monkeypatch.setattr(hv, "dense_hits", fake_hits)
    assert await hv.verify_claim(_claim(staged), config, ("320n", "", False, False)) == "hit"
    (c,) = await db.get_hayaa_claims(["g1"])
    assert c["verify_state"] == "hit" and c["verify_family"] == "nudenet" and c["verify_conf"] == 0.83
    assert c["verify_thumb"].endswith(".jpg")
    # The window is the claim plus the margin, and never grows.
    assert seen["start_ms"] == 55_000 and seen["last_ms"] == 75_000 and seen["gap_ms"] == 0


async def test_miss_is_recorded(staged, config, monkeypatch):
    async def no_hits(**kw):
        return []

    monkeypatch.setattr(hv, "dense_hits", no_hits)
    assert await hv.verify_claim(_claim(staged), config, ("320n", "", False, False)) == "miss"
    (c,) = await db.get_hayaa_claims(["g1"])
    assert c["verify_state"] == "miss"


async def test_disputed_hit_asks_the_false_positive_model(staged, config, monkeypatch):
    async def hits(**kw):
        return [(65_000, True, 0.9, ["FEMALE_BREAST_EXPOSED"], b"jpeg")]

    async def infer(fn, *args):
        return (True, 0.99)

    monkeypatch.setattr(hv, "dense_hits", hits)
    monkeypatch.setattr(scanner, "_infer", infer)
    await hv.verify_claim(_claim(staged, status="disputed"), config, ("320n", "", False, False))
    (c,) = await db.get_hayaa_claims(["g1"])
    assert c["verify_fp_drop"] == 1


async def test_missing_file_is_offline_and_overlong_window_is_not_checked(staged, config, tmp_path):
    assert await hv.verify_claim(_claim(tmp_path / "gone.mkv"), config, ("320n", "", False, False)) == "offline"
    long = _claim(staged, end_ms=60_000 + hv.MAX_WINDOW_MS + 1)
    assert await hv.verify_claim(long, config, ("320n", "", False, False)) == "na"


async def test_verify_pending_rechecks_waiting_claims_then_applies(staged, monkeypatch):
    await db.update_hayaa_claim(TK, "c1", verify_state="pending")
    applied = []

    async def prep(config, label):
        return ("320n", "", False, False)

    async def fake_verify(claim, config, detectors):
        await db.update_hayaa_claim(claim["title_key"], claim["claim_id"], verify_state="hit")
        return "hit"

    async def fake_apply(guids, config=None):
        applied.extend(guids)
        return {}

    async def cfg():
        return SimpleNamespace(is_scan_window=lambda: True)

    async def active():
        return True

    monkeypatch.setattr(scanner, "prepare_detectors", prep)
    monkeypatch.setattr(hv, "verify_claim", fake_verify)
    monkeypatch.setattr(hv.hayaa_sync, "apply_titles", fake_apply)
    assert await hv.verify_pending(cfg, active) == 1
    assert applied == ["g1"] and await db.count_hayaa_claims(verify_state="pending") == 0


async def test_verify_pending_stops_when_every_file_is_offline(staged, monkeypatch):
    await db.update_hayaa_claim(TK, "c1", verify_state="pending")

    async def prep(config, label):
        return ("320n", "", False, False)

    async def offline(claim, config, detectors):
        return "offline"

    async def cfg():
        return SimpleNamespace()

    async def active():
        return True

    monkeypatch.setattr(scanner, "prepare_detectors", prep)
    monkeypatch.setattr(hv, "verify_claim", offline)
    assert await hv.verify_pending(cfg, active) == 0
    assert await db.count_hayaa_claims(verify_state="pending") == 1
