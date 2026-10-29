"""Shared onnxruntime session options that keep scan workers from oversubscribing the CPU."""

from __future__ import annotations

import os

from .logger import get_logger

logger = get_logger(__name__)

# Set by configure() when the scanner pool starts; 0 means "not configured yet".
_intra_op_threads = 0


def auto_threads(workers: int) -> int:
    """Threads per session that leave about one worker's share of cores for ffmpeg, Plex and the web UI."""
    return max(1, (os.cpu_count() or 4) // (max(1, workers) + 1))


def configure(workers: int, threads: int = 0) -> int:
    """Set the per-session thread cap (threads=0: derive from workers); return it.

    onnxruntime defaults to one thread per core in *every* session, so N workers
    run N x cores threads and starve everything else on the host (#123). Sessions
    already built keep their old cap; the scanner pool restart rebuilds them.
    """
    global _intra_op_threads
    _intra_op_threads = threads if threads > 0 else auto_threads(workers)
    return _intra_op_threads


def intra_op_threads() -> int:
    return _intra_op_threads or auto_threads(2)


def session_options():
    """SessionOptions with intra-op threads capped and no inter-op parallelism."""
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = intra_op_threads()
    options.inter_op_num_threads = 1
    return options


def limit_nudenet_threads(detector, model_path: str = "") -> None:
    """Replace NudeDetector's session with a thread-capped one.

    NudeDetector builds its InferenceSession internally with no options hook, so the
    session is rebuilt from the same file after construction.
    """
    try:
        import onnxruntime as ort

        if not model_path:
            import nudenet

            model_path = os.path.join(os.path.dirname(nudenet.__file__), "320n.onnx")
        detector.onnx_session = ort.InferenceSession(model_path, sess_options=session_options())
    except Exception as exc:
        # The uncapped session NudeDetector built still works; only CPU use suffers.
        logger.warning("Could not cap NudeNet threads: %s", exc)
