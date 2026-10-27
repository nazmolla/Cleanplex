"""Unit tests for the SigLIP fallback scorer (issue #83). No model weights needed."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from cleanplex import siglip_detector as sd

DEFAULT_LABELS = {"FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED",
                  "ANUS_EXPOSED", "BUTTOCKS_EXPOSED"}


def _jpeg(w, h, color=(255, 255, 255)):
    ok, buf = cv2.imencode(".jpg", np.full((h, w, 3), color, np.uint8))
    assert ok
    return buf.tobytes()


def test_prompts_follow_enabled_labels():
    prompts, owner = sd.build_prompts({"FEMALE_BREAST_EXPOSED"})
    by_prompt = dict(zip(prompts, owner))

    assert by_prompt["a topless woman"] == "FEMALE_BREAST_EXPOSED"
    assert by_prompt["a shirtless man"] == ""
    assert by_prompt["a person wearing clothes"] == ""


def test_disabled_labels_become_counter_examples_without_changing_prompts():
    with_prompts, with_male = sd.build_prompts(DEFAULT_LABELS | {"MALE_BREAST_EXPOSED"})
    prompts, without_male = sd.build_prompts(DEFAULT_LABELS)
    i = prompts.index("a shirtless man")

    assert with_male[i] == "MALE_BREAST_EXPOSED" and without_male[i] == ""
    # Same prompt list either way, so cached text embeddings stay valid.
    assert with_prompts == prompts


def test_every_phrase_maps_to_an_existing_nudenet_label():
    """No SigLIP-only tags: each phrase belongs to exactly one NudeNet label."""
    from nudenet.nudenet import __labels as nudenet_labels

    assert set(sd.LABEL_PROMPTS) <= set(nudenet_labels)
    phrases = [p for ps in sd.LABEL_PROMPTS.values() for p in ps]
    assert len(phrases) == len(set(phrases))
    _, owner = sd.build_prompts(set(nudenet_labels))
    assert {o for o in owner if o} <= set(nudenet_labels)


def test_preprocess_pads_wide_frames_to_a_square():
    pixels = sd.preprocess(_jpeg(640, 360))

    assert pixels.shape == (1, 3, 224, 224)
    assert pixels.dtype == np.float32
    # White frame maps to ~1.0; padding above and below stays black (-1.0), and
    # nothing is cropped from the sides.
    assert pixels[0, :, 112, :].min() > 0.9
    assert pixels[0, :, 0, 112].max() < -0.9


def _fake_model(monkeypatch, owners, image):
    """Four prompts with one-hot embeddings; `image` picks which ones the frame matches."""
    monkeypatch.setattr(sd, "build_prompts", lambda labels: (["p0", "p1", "p2", "p3"], owners))
    monkeypatch.setattr(sd, "_prompt_embeddings", lambda prompts: np.eye(4, dtype=np.float32))

    class FakeVision:
        def run(self, outputs, feeds):
            return [np.array([image], np.float32)]

    monkeypatch.setattr(sd, "_session", lambda name: FakeVision())


def test_score_is_the_softmax_share_on_enabled_label_prompts(monkeypatch):
    _fake_model(monkeypatch, ["FEMALE_BREAST_EXPOSED", "", "", ""], [1.0, 0.0, 0.0, 0.0])

    score, label = sd.score_frame(_jpeg(64, 64), DEFAULT_LABELS)

    assert score == pytest.approx(1 / (1 + 3 * np.exp(-sd.LOGIT_SCALE)))
    assert label == "FEMALE_BREAST_EXPOSED"


def test_hit_is_tagged_with_the_label_holding_the_largest_share(monkeypatch):
    owners = ["FEMALE_BREAST_EXPOSED", "BUTTOCKS_EXPOSED", "BUTTOCKS_EXPOSED", ""]
    _fake_model(monkeypatch, owners, [0.6, 0.8, 0.0, 0.0])

    score, label = sd.score_frame(_jpeg(64, 64), DEFAULT_LABELS)

    assert label == "BUTTOCKS_EXPOSED"
    assert score == pytest.approx(1.0, abs=0.01)


def test_score_is_zero_with_no_enabled_labels(monkeypatch):
    monkeypatch.setattr(sd, "_session", lambda name: pytest.fail("model must not run"))

    assert sd.score_frame(_jpeg(64, 64), set()) == (0.0, "")


def test_score_is_zero_for_an_undecodable_frame(monkeypatch):
    monkeypatch.setattr(sd, "_prompt_embeddings", lambda prompts: np.eye(len(prompts), dtype=np.float32))

    assert sd.score_frame(b"not a jpeg", DEFAULT_LABELS) == (0.0, "")


async def test_ensure_models_reports_failure_without_raising(monkeypatch, tmp_path):
    monkeypatch.setattr(sd, "SIGLIP_DIR", tmp_path)
    monkeypatch.setattr(sd, "_download_models", lambda: False)

    assert await sd.ensure_models_async() is False
