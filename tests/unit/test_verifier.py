"""Tests for the feedback-trained false-positive verifier (#165)."""

import numpy as np
import pytest

from cleanplex import database as db
from cleanplex import siglip_detector, verifier

DIM = 8


def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def _data(n_each=60, seed=0):
    """False positives cluster around one direction, kept frames around another, across many titles."""
    rng = np.random.default_rng(seed)
    a, b = np.eye(DIM)[0], np.eye(DIM)[1]
    X = np.array([_unit(a + rng.normal(0, 0.4, DIM)) for _ in range(n_each)]
                 + [_unit(b + rng.normal(0, 0.4, DIM)) for _ in range(n_each)])
    y = np.array([1.0] * n_each + [0.0] * n_each)
    titles = [f"t{i % 12}" for i in range(2 * n_each)]
    return X, y, titles


@pytest.fixture
def model_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "cleanplex.db")
    monkeypatch.setattr(verifier, "_cache", None)
    return tmp_path


def test_fit_group_separates_two_clusters_with_ordered_cutoffs():
    X, y, titles = _data()
    m = verifier.fit_group(X, y, titles)
    assert m["auc"] > 0.9
    t = m["thresholds"]
    assert t[5] > t[10] > t[20]  # a stricter loss budget means a higher bar to drop


def test_fit_group_returns_none_when_a_class_is_too_small():
    X, y, titles = _data(n_each=10)
    assert verifier.fit_group(X, y, titles) is None


def test_should_drop_is_off_without_a_model_or_level(model_dir):
    assert verifier.should_drop(_unit(np.eye(DIM)[0]), "siglip", 10) == (False, None)
    X, y, titles = _data()
    verifier.save_model({"siglip": verifier.fit_group(X, y, titles)})
    assert verifier.should_drop(_unit(np.eye(DIM)[0]), "siglip", 0) == (False, None)
    assert verifier.should_drop(_unit(np.eye(DIM)[0]), "nudenet", 10) == (False, None)


def test_saved_model_drops_false_positive_like_frames_and_keeps_others(model_dir):
    X, y, titles = _data()
    verifier.save_model({"siglip": verifier.fit_group(X, y, titles)})
    drop_fp, _ = verifier.should_drop(_unit(np.eye(DIM)[0]), "siglip", 20)
    drop_kept, _ = verifier.should_drop(_unit(np.eye(DIM)[1]), "siglip", 20)
    assert drop_fp is True and drop_kept is False


def test_judge_jpeg_keeps_a_frame_it_cannot_embed(monkeypatch):
    def boom(_):
        raise ValueError("bad frame")

    monkeypatch.setattr(siglip_detector, "embed_frame", boom)
    assert verifier.judge_jpeg(b"x", "siglip", 10) == (False, None)


async def test_train_builds_a_model_from_feedback_and_kept_segments(setup_db, tmp_path, monkeypatch):
    monkeypatch.setattr(verifier, "_cache", None)
    monkeypatch.setattr(siglip_detector, "embed_frame", lambda b: np.frombuffer(b, dtype=np.float32))

    async def models_ok():
        return True

    monkeypatch.setattr(siglip_detector, "ensure_models_async", models_ok)
    rng = np.random.default_rng(1)
    for i in range(40):
        guid = f"g{i % 10}"
        for n, fp in enumerate((True, False)):
            base = np.eye(DIM)[0 if fp else 1]
            f = tmp_path / f"{'fp' if fp else 'ok'}{i}.jpg"
            f.write_bytes(_unit(base + rng.normal(0, 0.3, DIM)).tobytes())
            start = i * 100_000 + n * 50_000  # apart, so the two never merge into one segment
            seg_id = await db.insert_segment(guid, "T", start, start + 500, 0.8, str(f), "BUTTOCKS_EXPOSED",
                                             source="siglip", detector="siglip-base-224")
            if fp:
                await db.mark_segment_false_positive(seg_id, str(f))

    status = await verifier.train()

    assert status["groups"]["siglip"]["auc"] > 0.9
    assert verifier.model_path().is_file()
    assert "nudenet" not in status["groups"]  # no nudenet examples, so no model for that family


async def test_train_fails_clearly_without_enough_examples(setup_db, monkeypatch):
    async def models_ok():
        return True

    monkeypatch.setattr(siglip_detector, "ensure_models_async", models_ok)
    with pytest.raises(RuntimeError, match="at least"):
        await verifier.train()
    assert verifier.status()["error"]
