"""Feedback-trained false-positive filter: a logistic head on SigLIP image embeddings (#165).

Trained from the frames users marked as false positives versus the thumbnails of segments they
kept, one model per detector family ("siglip" hits and "nudenet" hits). At scan time a flagged
cluster's peak frame is scored and dropped when it looks like the frames users rejected.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import numpy as np

from . import database as db
from . import siglip_detector
from .logger import get_logger

logger = get_logger(__name__)

GROUPS = ("siglip", "nudenet")
# Share of kept segments the user accepts losing; each level has its own score cut-off.
LEVELS = (5, 10, 20)
FOLDS = 5
MIN_PER_CLASS = 30
MAX_KEPT_PER_GROUP = 1500  # bounds training time: one SigLIP run per frame
EMBED_SCALE = 10.0  # embeddings are unit length, so their weights need a larger scale to fit quickly

_lock = asyncio.Lock()  # one training run at a time
_state: dict = {"running": False, "progress": 0.0, "error": "", "finished_at": None}
_cache: tuple[float, dict] | None = None  # (file mtime, loaded model)


def model_path() -> Path:
    return db.get_db_path().parent / "models" / "verifier.npz"


def _fit(X: np.ndarray, y: np.ndarray, l2: float = 1e-3, iters: int = 300, lr: float = 2.0) -> tuple[np.ndarray, float]:
    """Class-balanced logistic regression by gradient descent; y is 1 for a false positive."""
    w = np.zeros(X.shape[1])
    b = 0.0
    cw = np.where(y == 1, 0.5 / y.mean(), 0.5 / (1 - y.mean()))
    cw = cw / cw.mean()
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(X @ w + b)))
        r = (p - y) * cw
        w -= lr * (X.T @ r / len(y) + l2 * w)
        b -= lr * r.mean()
    return w, float(b)


def _auc(scores: np.ndarray, y: np.ndarray) -> float:
    order = np.argsort(scores)
    ranks = np.empty(len(scores))
    ranks[order] = np.arange(len(scores))
    pos, neg = int(y.sum()), len(y) - int(y.sum())
    return float((ranks[y == 1].sum() - pos * (pos - 1) / 2) / (pos * neg))


def fit_group(X: np.ndarray, y: np.ndarray, titles: list[str]) -> dict | None:
    """Fit one detector family; None when either class is too small.

    Cut-offs and AUC come from out-of-fold scores with folds grouped by title, so neighbouring
    frames of one film never sit on both sides of a split.
    """
    if int(y.sum()) < MIN_PER_CLASS or int((1 - y).sum()) < MIN_PER_CLASS:
        return None
    X = X * EMBED_SCALE
    unique = sorted(set(titles))
    np.random.default_rng(0).shuffle(unique)
    fold_of = {t: i % FOLDS for i, t in enumerate(unique)}
    folds = np.array([fold_of[t] for t in titles])
    oof = np.zeros(len(y))
    for k in range(FOLDS):
        train = folds != k
        if y[train].sum() == 0 or (1 - y[train]).sum() == 0:
            return None
        w, b = _fit(X[train], y[train])
        oof[~train] = X[~train] @ w + b
    kept = oof[y == 0]
    w, b = _fit(X, y)
    return {
        "w": w, "b": b,
        "thresholds": {level: float(np.quantile(kept, 1 - level / 100)) for level in LEVELS},
        "auc": _auc(oof, y), "n": len(y), "false_positives": int(y.sum()),
    }


def save_model(models: dict[str, dict]) -> None:
    path = model_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    arrays: dict[str, np.ndarray] = {}
    meta: dict = {"trained_at": time.time(), "groups": {}}
    for group, m in models.items():
        arrays[f"{group}_w"] = m["w"]
        arrays[f"{group}_b"] = np.array([m["b"]])
        meta["groups"][group] = {
            "thresholds": {str(k): v for k, v in m["thresholds"].items()},
            "auc": m["auc"], "n": m["n"], "false_positives": m["false_positives"],
        }
    arrays["meta"] = np.array(json.dumps(meta))
    tmp = path.with_suffix(".tmp.npz")
    np.savez(tmp, **arrays)
    tmp.replace(path)  # atomic: a scan never loads a half-written file


def load_model() -> dict | None:
    """Return {group: {w, b, thresholds, auc, n, false_positives}, "trained_at": ts} or None; cached by mtime."""
    global _cache
    path = model_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _cache and _cache[0] == mtime:
        return _cache[1]
    with np.load(path) as data:
        meta = json.loads(str(data["meta"]))
        model: dict = {"trained_at": meta["trained_at"]}
        for group, info in meta["groups"].items():
            model[group] = {
                "w": data[f"{group}_w"], "b": float(data[f"{group}_b"][0]),
                "thresholds": {int(k): v for k, v in info["thresholds"].items()},
                "auc": info["auc"], "n": info["n"], "false_positives": info["false_positives"],
            }
    _cache = (mtime, model)
    return model


def status() -> dict:
    """Training state plus what the saved model covers, for the settings page."""
    model = load_model()
    groups = {g: {k: model[g][k] for k in ("auc", "n", "false_positives")} for g in GROUPS if model and g in model}
    return {**_state, "trained_at": model["trained_at"] if model else None, "groups": groups}


def should_drop(embedding: np.ndarray, group: str, loss_pct: int) -> tuple[bool, float | None]:
    """Return (drop, score) for one peak-frame embedding; never drops when the level is off or no model covers the group."""
    model = load_model()
    if loss_pct not in LEVELS or not model or group not in model:
        return False, None
    m = model[group]
    score = float(embedding * EMBED_SCALE @ m["w"] + m["b"])
    return score > m["thresholds"][loss_pct], score


def judge_jpeg(jpeg: bytes, group: str, loss_pct: int) -> tuple[bool, float | None]:
    """Blocking: embed a frame and decide. A frame that cannot be embedded is kept."""
    try:
        return should_drop(siglip_detector.embed_frame(jpeg), group, loss_pct)
    except Exception as exc:
        logger.debug("Verifier could not score a frame: %s", exc)
        return False, None


def _embed_all(rows: list[dict]) -> tuple[list[dict], np.ndarray]:
    """Blocking: embed every readable frame, updating progress; unreadable files are skipped."""
    kept_rows, vectors = [], []
    for i, row in enumerate(rows):
        try:
            vectors.append(siglip_detector.embed_frame(Path(row["path"]).read_bytes()))
            kept_rows.append(row)
        except Exception:
            continue
        _state["progress"] = (i + 1) / len(rows)
    return kept_rows, np.array(vectors)


async def train() -> dict:
    """Embed the labelled frames, fit both groups and save the model; returns the new status.

    Raises RuntimeError if a run is already going, SigLIP's models are missing, or no group has
    enough examples of both kinds.
    """
    if _lock.locked():
        raise RuntimeError("Training is already running")
    async with _lock:
        _state.update(running=True, progress=0.0, error="")
        try:
            if not await siglip_detector.ensure_models_async():
                raise RuntimeError("SigLIP models are not available")
            rows = await db.get_verifier_training_rows(MAX_KEPT_PER_GROUP)
            rows, vectors = await asyncio.to_thread(_embed_all, rows)
            models: dict[str, dict] = {}
            for group in GROUPS:
                idx = [i for i, r in enumerate(rows) if r["group"] == group]
                if not idx:
                    continue
                fitted = await asyncio.to_thread(
                    fit_group, vectors[idx], np.array([rows[i]["fp"] for i in idx], dtype=float),
                    [rows[i]["plex_guid"] for i in idx],
                )
                if fitted:
                    models[group] = fitted
            if not models:
                raise RuntimeError(f"Need at least {MIN_PER_CLASS} false positives and kept frames per detector to train")
            await asyncio.to_thread(save_model, models)
            _state["finished_at"] = time.time()
            logger.info("Verifier trained: %s", {g: (m["n"], round(m["auc"], 3)) for g, m in models.items()})
        except Exception as exc:
            _state["error"] = str(exc)
            raise
        finally:
            _state["running"] = False
    return status()
