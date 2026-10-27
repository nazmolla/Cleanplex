"""SigLIP zero-shot frame scorer: the fallback that catches frames NudeNet misses.

NudeNet looks for body-part boxes and finds nothing in many wide, dim or partly
occluded film frames. SigLIP instead judges the whole frame against short text
descriptions, which is cheap at 224 px and catches a different set of scenes. It
only runs on frames NudeNet did not flag (see scanner.scan_video).

How a frame is judged: the image is embedded once, then compared with every prompt
in one matrix product. A softmax over all prompts turns the similarities into a
probability distribution, and the frame's score is the share that lands on prompts
for labels the user enabled in scan_labels. Prompts for labels the user left out
(e.g. MALE_BREAST_EXPOSED) and neutral scene prompts compete for that probability,
so a shirtless man or a talking-heads shot pulls the score down rather than up.

Every phrase belongs to exactly one NudeNet label, so a SigLIP hit is tagged with an
existing label (the enabled one with the largest share) and never a label of its own.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import threading
from pathlib import Path

import numpy as np

from .logger import get_logger

logger = get_logger(__name__)

MODEL_REPO = "Xenova/siglip-base-patch16-224"
_BASE_URL = f"https://huggingface.co/{MODEL_REPO}/resolve/main"
# Published path -> local filename. fp32 weights: the int8 exports trade recall
# for size, and recall is the whole point of this stage. ~815 MB, downloaded once.
_MODEL_FILES = {
    "onnx/vision_model.onnx": "vision_model.onnx",
    "onnx/text_model.onnx": "text_model.onnx",
    "tokenizer.json": "tokenizer.json",
}
SIGLIP_DIR: Path = Path.home() / ".cleanplex" / "models" / "siglip"

IMAGE_SIZE = 224
TEXT_LENGTH = 64  # SigLIP was trained on text padded to exactly 64 tokens
PAD_ID = 1  # "</s>", SigLIP's pad and eos token
# exp(logit_scale) from the google/siglip-base-patch16-224 checkpoint. The split
# ONNX export drops it, and it sets how peaked the softmax is, so the threshold
# means nothing without it. The checkpoint's logit_bias cancels out of a softmax.
LOGIT_SCALE = math.exp(4.764997)

_DOWNLOAD_ATTEMPTS = 2

# Phrases per NudeNet label. Enabled labels score as hits; the rest compete as
# counter-examples, so the prompt set follows the user's scan_labels. Whole-scene
# phrases (sex scenes rarely show one body part clearly) sit under the label they
# most imply rather than forming a group of their own.
LABEL_PROMPTS: dict[str, list[str]] = {
    "FEMALE_BREAST_EXPOSED": ["a topless woman", "a woman with bare breasts", "a naked woman in bed"],
    "FEMALE_GENITALIA_EXPOSED": [
        "a fully naked woman", "a nude woman showing her genitals", "full frontal female nudity",
        "a woman having sex",
    ],
    "MALE_GENITALIA_EXPOSED": [
        "a fully naked man", "a nude man showing his genitals", "full frontal male nudity",
        "a man having sex",
    ],
    "BUTTOCKS_EXPOSED": [
        "a person with bare buttocks", "a naked person seen from behind", "a naked couple having sex",
    ],
    "ANUS_EXPOSED": ["a naked person bending over showing their anus"],
    "MALE_BREAST_EXPOSED": ["a shirtless man", "a man with a bare chest"],
    "BELLY_EXPOSED": ["a person showing a bare belly", "a woman in a crop top"],
    "ARMPITS_EXPOSED": ["a person with raised bare arms"],
    "FEET_EXPOSED": ["bare feet", "a person barefoot"],
    "FEMALE_BREAST_COVERED": ["a woman in a bikini top", "a woman in a low-cut top"],
    "FEMALE_GENITALIA_COVERED": ["a woman in underwear", "a woman in a bikini"],
    "BUTTOCKS_COVERED": ["a person in tight shorts", "a person in a swimsuit seen from behind"],
    "ANUS_COVERED": ["a person in a swimsuit bending over"],
    "BELLY_COVERED": ["a person in a t-shirt"],
    "ARMPITS_COVERED": ["a person in a long-sleeved shirt"],
    "FEET_COVERED": ["a person wearing shoes"],
    "FACE_FEMALE": ["a close-up of a woman's face"],
    "FACE_MALE": ["a close-up of a man's face"],
}

# Always counter-examples: the ordinary frames a film is mostly made of.
NEUTRAL_PROMPTS = [
    "people in clothes talking",
    "a couple fully clothed in bed",
    "a person wearing clothes",
    "a crowd of people",
    "a landscape",
    "a city street",
    "the inside of a room",
    "a car",
    "text on a screen",
    "a dark scene",
    "an action scene",
    "people kissing with clothes on",
]

_thread_local = threading.local()
# threading.Lock, not asyncio.Lock: prompt embeddings are built inside the
# to_thread workers that run score_frame, never on the event loop.
_embed_lock = threading.Lock()
_prompt_cache: dict[str, np.ndarray] = {}
_download_lock = asyncio.Lock()


def build_prompts(enabled_labels: set[str]) -> tuple[list[str], list[str]]:
    """Return (prompts, owner) where owner[i] is prompt i's label if enabled, else "".

    Prompts never change with enabled_labels, only which of them count as hits, so
    the cached text embeddings stay valid when the user edits scan_labels.
    """
    prompts: list[str] = []
    owner: list[str] = []
    for label, phrases in LABEL_PROMPTS.items():
        prompts += phrases
        owner += [label if label in enabled_labels else ""] * len(phrases)
    prompts += NEUTRAL_PROMPTS
    owner += [""] * len(NEUTRAL_PROMPTS)
    return prompts, owner


def models_present() -> bool:
    return all((SIGLIP_DIR / name).is_file() for name in _MODEL_FILES.values())


def _download_models() -> bool:
    """Blocking download of any missing model file; call via asyncio.to_thread."""
    import httpx

    SIGLIP_DIR.mkdir(parents=True, exist_ok=True)
    for remote, name in _MODEL_FILES.items():
        target = SIGLIP_DIR / name
        if target.is_file() and target.stat().st_size > 0:
            continue
        for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
            tmp = target.with_suffix(target.suffix + ".part")
            try:
                logger.info("Downloading SigLIP %s (attempt %d)", remote, attempt)
                with httpx.stream("GET", f"{_BASE_URL}/{remote}", follow_redirects=True, timeout=60) as r:
                    r.raise_for_status()
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_bytes(1 << 20):
                            f.write(chunk)
                tmp.replace(target)
                break
            except Exception as exc:
                tmp.unlink(missing_ok=True)
                logger.warning("SigLIP download of %s failed: %s", remote, exc)
        else:
            return False
    return True


async def ensure_models_async() -> bool:
    """Make sure the model files are on disk; False if they could not be fetched."""
    if models_present():
        return True
    async with _download_lock:
        if models_present():
            return True
        return await asyncio.to_thread(_download_models)


def _session(name: str):
    """One onnxruntime session per model per worker thread, like the NudeNet detector."""
    sessions = getattr(_thread_local, "sessions", None)
    if sessions is None:
        sessions = _thread_local.sessions = {}
    if name not in sessions:
        import onnxruntime as ort

        sessions[name] = ort.InferenceSession(
            str(SIGLIP_DIR / name), providers=["CPUExecutionProvider"]
        )
    return sessions[name]


def _normalize(x: np.ndarray) -> np.ndarray:
    return x / np.linalg.norm(x, axis=-1, keepdims=True)


def _embed_texts(prompts: list[str]) -> np.ndarray:
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(str(SIGLIP_DIR / "tokenizer.json"))
    ids = np.full((len(prompts), TEXT_LENGTH), PAD_ID, dtype=np.int64)
    for row, text in enumerate(prompts):
        # The SigLIP tokenizer lowercases (do_lower_case); the encoding ends in </s>.
        tokens = tokenizer.encode(text.lower()).ids[:TEXT_LENGTH]
        ids[row, : len(tokens)] = tokens
    out = _session("text_model.onnx").run(["pooler_output"], {"input_ids": ids})[0]
    return _normalize(out.astype(np.float32))


def _prompt_embeddings(prompts: list[str]) -> np.ndarray:
    """Return normalized prompt embeddings, cached in memory and on disk.

    The text model only runs when this module's phrases change.
    """
    key = hashlib.sha256(json.dumps([MODEL_REPO, prompts]).encode()).hexdigest()[:16]
    with _embed_lock:
        if key not in _prompt_cache:
            cache_file = SIGLIP_DIR / f"prompts-{key}.npy"
            if cache_file.is_file():
                embeddings = np.load(cache_file)
            else:
                embeddings = _embed_texts(prompts)
                np.save(cache_file, embeddings)
            _prompt_cache[key] = embeddings
        return _prompt_cache[key]


def preprocess(jpeg_bytes: bytes) -> np.ndarray:
    """Decode a JPEG into SigLIP's (1, 3, 224, 224) input.

    The frame is padded to a square rather than centre-cropped: a crop of a 16:9
    frame throws away the sides, which is often where the people are.
    """
    import cv2

    mat = cv2.imdecode(np.frombuffer(jpeg_bytes, np.uint8), cv2.IMREAD_COLOR)
    if mat is None:
        raise ValueError("could not decode frame")
    h, w = mat.shape[:2]
    side = max(h, w)
    top, left = (side - h) // 2, (side - w) // 2
    mat = cv2.copyMakeBorder(mat, top, side - h - top, left, side - w - left, cv2.BORDER_CONSTANT)
    mat = cv2.resize(mat, (IMAGE_SIZE, IMAGE_SIZE), interpolation=cv2.INTER_CUBIC)
    rgb = cv2.cvtColor(mat, cv2.COLOR_BGR2RGB).astype(np.float32)
    # SigLIP normalization: mean 0.5, std 0.5 after scaling to [0, 1], i.e. [-1, 1].
    chw = (rgb / 127.5 - 1.0).transpose(2, 0, 1)
    return chw[np.newaxis]


def score_frame(jpeg_bytes: bytes, enabled_labels: set[str]) -> tuple[float, str]:
    """Return (score, label) for one frame.

    score is the probability share on enabled-label prompts (0..1); label is the
    enabled NudeNet label holding the largest share of it. Blocking; call via
    asyncio.to_thread. Returns (0.0, "") if the frame cannot be scored.
    """
    prompts, owner = build_prompts(enabled_labels)
    if not any(owner):
        return 0.0, ""
    try:
        embeddings = _prompt_embeddings(prompts)
        image = _session("vision_model.onnx").run(["pooler_output"], {"pixel_values": preprocess(jpeg_bytes)})[0]
        logits = LOGIT_SCALE * (_normalize(image.astype(np.float32))[0] @ embeddings.T)
        probs = np.exp(logits - logits.max())
        probs /= probs.sum()
    except Exception as exc:
        logger.debug("SigLIP scoring error: %s", exc)
        return 0.0, ""
    by_label: dict[str, float] = {}
    for prob, label in zip(probs, owner):
        if label:
            by_label[label] = by_label.get(label, 0.0) + float(prob)
    label = max(by_label, key=by_label.get)
    return sum(by_label.values()), label
