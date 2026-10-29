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
    prompts, owner, _ = sd.build_prompts({"FEMALE_BREAST_EXPOSED"})
    by_prompt = dict(zip(prompts, owner))

    assert by_prompt["bare female breasts"] == "FEMALE_BREAST_EXPOSED"
    assert by_prompt["a man's bare chest"] == ""
    assert by_prompt["a person wearing clothes"] == ""


def test_disabled_labels_become_counter_examples_without_changing_prompts():
    with_prompts, with_male, _ = sd.build_prompts(DEFAULT_LABELS | {"MALE_BREAST_EXPOSED"})
    prompts, without_male, _ = sd.build_prompts(DEFAULT_LABELS)
    i = prompts.index("a man's bare chest")

    assert with_male[i] == "MALE_BREAST_EXPOSED" and without_male[i] == ""
    # Same prompt list either way, so cached text embeddings stay valid.
    assert with_prompts == prompts


def test_every_phrase_maps_to_an_existing_nudenet_label():
    """No SigLIP-only tags: each phrase belongs to exactly one NudeNet label."""
    from nudenet.nudenet import __labels as nudenet_labels

    assert set(sd.LABEL_PROMPTS) <= set(nudenet_labels)
    phrases = [p for ps in sd.LABEL_PROMPTS.values() for p in ps]
    assert len(phrases) == len(set(phrases))
    _, owner, _ = sd.build_prompts(set(nudenet_labels))
    assert {o for o in owner if o} <= set(nudenet_labels)


def test_label_phrases_name_a_body_part_not_a_scene():
    """#96: a tag must describe what it says, e.g. MALE_GENITALIA_EXPOSED is a penis."""
    scene_words = ("sex", "couple", "in bed", "fully naked", "full frontal", "person")
    for label, phrases in sd.LABEL_PROMPTS.items():
        for phrase in phrases:
            assert not any(w in phrase for w in scene_words), (label, phrase)
    assert "a penis" in sd.LABEL_PROMPTS["MALE_GENITALIA_EXPOSED"]


def test_scene_phrases_score_but_never_own_a_label():
    prompts, owner, positive = sd.build_prompts(DEFAULT_LABELS)
    i = prompts.index("people having sex")

    assert positive[i] and owner[i] == ""
    _, _, positive_off = sd.build_prompts({"FEET_EXPOSED"})
    assert not positive_off[i]


def test_preprocess_pads_wide_frames_to_a_square():
    pixels = sd.preprocess(_jpeg(640, 360))

    assert pixels.shape == (1, 3, 224, 224)
    assert pixels.dtype == np.float32
    # White frame maps to ~1.0; padding above and below stays black (-1.0), and
    # nothing is cropped from the sides.
    assert pixels[0, :, 112, :].min() > 0.9
    assert pixels[0, :, 0, 112].max() < -0.9


def _fake_model(monkeypatch, owners, image, positive=None):
    """Four prompts with one-hot embeddings; `image` picks which ones the frame matches."""
    positive = positive if positive is not None else [bool(o) for o in owners]
    monkeypatch.setattr(sd, "build_prompts", lambda labels: (["p0", "p1", "p2", "p3"], owners, positive))
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


def test_a_scene_match_raises_the_score_but_the_tag_comes_from_body_parts(monkeypatch):
    # p0 = breasts phrase, p1 = buttocks phrase, p2 = scene phrase (no owner), p3 = neutral.
    _fake_model(monkeypatch, ["FEMALE_BREAST_EXPOSED", "BUTTOCKS_EXPOSED", "", ""],
                [0.2, 0.25, 0.9, 0.0], positive=[True, True, True, False])

    score, label = sd.score_frame(_jpeg(64, 64), DEFAULT_LABELS)

    assert score == pytest.approx(1.0, abs=0.01)  # the scene phrase dominates the score
    assert label == "BUTTOCKS_EXPOSED"              # but the tag is the best body part


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


def test_credit_and_title_card_phrases_are_neutral_counter_examples():
    """#122: text-heavy credit screens need prompts to land on that are never hits."""
    prompts, owner, positive = sd.build_prompts({"BUTTOCKS_EXPOSED"})
    for phrase in ("end credits scrolling text", "a film title card", "white text on a black background"):
        i = prompts.index(phrase)
        assert owner[i] == "" and positive[i] is False
